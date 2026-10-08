"""
Channel-pair picker -- runs as its OWN process so the web server's GIL can never stall
audio capture (v1 sliced inside the Flask process; heavy fader traffic caused dropouts).

  usage: picker.py <ctl_file> <capture cmd ...>      (env PICKER_CHANNELS = channels in the capture, default 48)

stdin : unused
stdout: raw stereo s24le 48 kHz (piped straight into ffmpeg by the parent)
stderr: one status line per ~100 ms ->  "S <peakL_dB> <peakR_dB> <overruns>"
        on capture failure           ->  "E <message>"   then exit 1
ctl   : text file "L R [SL SR GAIN]" (zero-based capture channel indices); re-read when its mtime changes.
        v3.9 sub blend: with SL SR GAIN (GAIN = linear, > 0) the output is
            L + GAIN*(SL+SR)/2 , R + GAIN*(SL+SR)/2      (saturating; a mono sub uses SL == SR)
        Needs numpy; without it the blend is ignored ("W" line on stderr) and plain L/R carries on.
        Gain changes ramp across one 10 ms block (no zipper noise); blend off ramps down, then the
        cheap strided copy takes over again.

Capture format: S24_3LE, 48 kHz, PICKER_CHANNELS channels (WING USB: 48; X32 X-LIVE USB: 32).

Cam output (v3.4, optional): when the PICKER_CAM_CTL environment variable names a file, that file may
hold one line  "L R delay_ms /path/to/fifo"  (or be missing / say "off"). While it is set the picker
also slices a SECOND stereo pair out of the same capture, delays it by delay_ms (sample-accurate, live
adjustable) and writes it to the fifo for the video encoder. The listen-back path above is written
FIRST and is never waited on: the cam side has its own bounded queue and writer thread and drops
audio when its reader stalls, so a stuck video encoder can't add a microsecond to the listen feed.
v4.2: an optional second line  "blend SL SR GAIN"  mixes the sub pair into the cam pair exactly like the
listen blend above (same ramp, same numpy routine; without numpy the cam pair stays plain L/R).
"""
import collections
import errno
import math
import os
import select
import subprocess
import sys
import threading
import time

try:
    import numpy as np
except Exception:                       # the blend needs it; plain listen never does
    np = None

CHANNELS = 48
FRAME = CHANNELS * 3
CHUNK = FRAME * 480                     # 10 ms
FULL = 8388608.0


def configure(channels):
    """Set the capture width (channels per frame)."""
    global CHANNELS, FRAME, CHUNK
    CHANNELS = max(2, int(channels))
    FRAME = CHANNELS * 3
    CHUNK = FRAME * 480


def _clamp(v):
    return max(0, min(CHANNELS - 1, int(v)))


def read_route(path, cur):
    """-> ((L, R), blend) where blend is (SL, SR, gain) or None. Keeps `cur` on a bad/missing file."""
    try:
        parts = open(path).read().split()
        pair = (_clamp(parts[0]), _clamp(parts[1]))
        blend = None
        if len(parts) >= 5:
            g = float(parts[4])
            if g > 0:
                blend = (_clamp(parts[2]), _clamp(parts[3]), min(g, 16.0))
        return pair, blend
    except Exception:
        return cur


def read_pair(path, cur):
    """Kept for callers/tests that only want the pair."""
    return read_route(path, (cur, None))[0]


def s24_to_i32(a):
    """uint8 array (..., 3) little-endian signed 24-bit -> int32 array (...)."""
    v = a[..., 0].astype(np.int32) | (a[..., 1].astype(np.int32) << 8) | (a[..., 2].astype(np.int32) << 16)
    return (v << 8) >> 8                                # sign-extend


def blend_block(buf, n, pair, sl, sr, g0, g1):
    """Stereo s24le of n frames: L/R plus the sub pair's average, gain ramped g0 -> g1, saturated."""
    a = np.frombuffer(buf, dtype=np.uint8, count=n * FRAME).reshape(n, CHANNELS, 3)
    v = s24_to_i32(a[:, [pair[0], pair[1], sl, sr], :])
    g = np.linspace(g0, g1, n, endpoint=False, dtype=np.float32) if g0 != g1 else np.float32(g1)
    sub = (v[:, 2].astype(np.float32) + v[:, 3].astype(np.float32)) * (np.float32(0.5) * g)
    out = np.empty((n, 2), dtype=np.float32)
    out[:, 0] = v[:, 0] + sub
    out[:, 1] = v[:, 1] + sub
    o = np.clip(np.rint(out), -8388608, 8388607).astype('<i4')
    return o.view(np.uint8).reshape(n, 2, 4)[:, :, :3].tobytes()


def peak_db(buf, off):
    pk = 0
    for i in range(off, len(buf) - 2, 6 * 8):
        v = abs(int.from_bytes(buf[i:i + 3], 'little', signed=True))
        if v > pk:
            pk = v
    return -120.0 if pk == 0 else max(-120.0, 20 * math.log10(pk / FULL))


MAX_DELAY_MS = 3000
CAM_QUEUE_S = 2.0                       # cam writer queue cap: older audio is dropped beyond this
BYTES_PER_S = 48000 * 6                 # stereo s24le @ 48 kHz


def parse_cam_ctl(path):
    """-> (L, R, delay_frames, fifo, blend) or None when the cam output is off.
    blend = (SL, SR, gain) from an optional 2nd line "blend SL SR GAIN" (gain linear, > 0), else None."""
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return None
    parts = lines[0].split(None, 3) if lines else []
    if len(parts) < 4 or parts[0] == 'off':
        return None
    blend = None
    for extra in lines[1:]:
        b = extra.split()
        if len(b) == 4 and b[0] == 'blend':
            try:
                g = float(b[3])
                if g > 0:
                    blend = (max(0, min(CHANNELS - 1, int(b[1]))), max(0, min(CHANNELS - 1, int(b[2]))), min(g, 16.0))
            except ValueError:
                pass
    try:
        l, r = int(parts[0]), int(parts[1])
        ms = max(0, min(MAX_DELAY_MS, int(float(parts[2]))))
    except ValueError:
        return None
    fifo = parts[3].strip()
    if not fifo:
        return None
    return (max(0, min(CHANNELS - 1, l)), max(0, min(CHANNELS - 1, r)), ms * 48, fifo, blend)


class DelayLine:
    """Constant-rate audio delay: process(data, delay_bytes) returns exactly len(data) bytes.
    While filling (start, or after the delay is raised) the missing part is silence; when the delay
    is lowered the surplus is skipped. Never repeats audio. Sizes are multiples of one 6-byte frame."""

    def __init__(self):
        self.q = collections.deque()
        self.len = 0

    def _pop(self, n):
        out = []
        while n > 0 and self.q:
            c = self.q[0]
            if len(c) <= n:
                out.append(self.q.popleft()); n -= len(c); self.len -= len(c)
            else:
                out.append(c[:n]); self.q[0] = c[n:]; self.len -= n; n = 0
        return b''.join(out)

    def process(self, data, delay_bytes):
        n = len(data)
        self.q.append(bytes(data)); self.len += n
        avail = self.len - delay_bytes
        if avail >= n:
            out = self._pop(n)
            if avail > n:                         # delay was lowered: skip the surplus
                self._pop(avail - n)
            return out
        have = max(0, avail)
        return bytes(n - have) + self._pop(have)


class CamOut:
    """Bounded, non-blocking-for-the-caller writer to the cam fifo."""

    def __init__(self, path):
        self.path = path
        self.fd = -1
        self.q = collections.deque()
        self.qbytes = 0
        self.cv = threading.Condition()
        self.closed = False
        self.dropped = 0
        self.last_try = 0.0
        threading.Thread(target=self._run, daemon=True, name='cam-out').start()

    def _open(self):
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)    # ENXIO until a reader exists
        except OSError:
            return False
        self.fd = fd
        return True

    def put(self, data):
        cap = int(CAM_QUEUE_S * BYTES_PER_S)
        with self.cv:
            if self.closed:
                return
            self.q.append(data); self.qbytes += len(data)
            while self.qbytes > cap and len(self.q) > 1:     # reader too slow: drop the oldest
                d = self.q.popleft(); self.qbytes -= len(d); self.dropped += len(d)
            self.cv.notify()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def _run(self):
        while True:
            with self.cv:
                while not self.q and not self.closed:
                    self.cv.wait(0.5)
                if self.closed:
                    break
                data = self.q.popleft(); self.qbytes -= len(data)
            if self.fd < 0 and not self._open():             # no reader yet (encoder still starting)
                time.sleep(0.2)
                continue                                     # this chunk is dropped; stay current
            mv, off = memoryview(data), 0
            while off < len(mv) and not self.closed:
                try:
                    off += os.write(self.fd, mv[off:])
                except BlockingIOError:
                    select.select([], [self.fd], [], 0.2)
                except OSError as e:                         # EPIPE: reader went away
                    if e.errno in (errno.EPIPE, errno.EBADF):
                        try:
                            os.close(self.fd)
                        except OSError:
                            pass
                        self.fd = -1
                    break
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def main():
    ctl, cmd = sys.argv[1], sys.argv[2:]
    err = sys.stderr
    configure(os.environ.get('PICKER_CHANNELS', '48') or 48)
    try:
        cap = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except OSError as e:
        err.write(f'E {e}\n'); err.flush(); sys.exit(1)

    overruns = [0]
    tail = []

    def drain():                         # arecord reports xruns on stderr
        for line in iter(cap.stderr.readline, b''):
            t = line.decode(errors='replace').strip()
            if 'overrun' in t.lower():
                overruns[0] += 1
            elif t:
                tail.append(t); del tail[:-3]
    threading.Thread(target=drain, daemon=True).start()

    out = sys.stdout.buffer
    pair, blend = read_route(ctl, ((0, 1), None))
    gain_now = 0.0                       # blend gain actually applied (ramps toward the target)
    sub = (0, 0)
    warned = False
    try:
        mtime = os.stat(ctl).st_mtime_ns
    except OSError:
        mtime = 0
    rest = b''
    last = 0.0
    cam_ctl = os.environ.get('PICKER_CAM_CTL', '')
    cam = None                           # (L, R, delay_frames, fifo) while the cam output is on
    cam_out, cam_delay = None, DelayLine()
    cam_mtime = None
    cam_gain, cam_sub = 0.0, (0, 0)      # v4.2 cam sub blend: gain actually applied (ramps like listen's)

    def cam_poll():
        """Apply the cam control file (cheap: one stat per 100 ms). Never raises."""
        nonlocal cam, cam_out, cam_mtime, cam_delay
        try:
            try:
                m = os.stat(cam_ctl).st_mtime_ns
            except OSError:
                m = 0
            if m == cam_mtime:
                return
            cam_mtime = m
            new = parse_cam_ctl(cam_ctl) if m else None
            if new is None or (cam and new[3] != cam[3]):
                if cam_out:
                    cam_out.close(); cam_out = None
                cam_delay = DelayLine()
            if new is not None and cam_out is None:
                cam_out = CamOut(new[3])
            cam = new
        except Exception as e:           # the cam side must never take the listen feed down
            cam = None
            err.write(f'C poll failed: {e}\n'); err.flush()

    if cam_ctl:
        cam_poll()
    while True:
        data = cap.stdout.read(CHUNK)
        if not data:
            cap.wait()
            err.write('E capture ended' + (': ' + ' | '.join(tail) if tail else '') + '\n')
            err.flush(); sys.exit(1)
        buf = rest + data
        n = len(buf) // FRAME
        rest = buf[n * FRAME:]
        target = blend[2] if blend else 0.0
        if blend:
            sub = blend[:2]
        if (target > 0 or gain_now > 0) and np is None:
            if not warned:
                warned = True
                err.write('W sub blend needs numpy -- playing plain L/R\n'); err.flush()
            target = gain_now = 0.0
        if target > 0 or gain_now > 0:
            try:
                st = blend_block(buf, n, pair, sub[0], sub[1], gain_now, target)
            except Exception as e:       # never let the blend take listen down
                err.write(f'W blend failed: {e}\n'); err.flush()
                blend, target = None, 0.0
                st = None
            gain_now = target
        else:
            st = None
        if st is None:
            lo, ro = pair[0] * 3, pair[1] * 3
            st = bytearray(n * 6)
            for k in range(3):           # C-speed strided copies, no per-sample Python
                st[k::6] = buf[lo + k:n * FRAME:FRAME]
                st[3 + k::6] = buf[ro + k:n * FRAME:FRAME]
        try:
            out.write(st); out.flush()
        except (BrokenPipeError, OSError):
            cap.kill(); sys.exit(0)
        if cam is not None and cam_out is not None:      # after the listen write: listen never waits on this
            try:
                cl, cr, dframes, _, cblend = cam
                ctarget = cblend[2] if (cblend and np is not None) else 0.0
                if cblend:
                    cam_sub = cblend[:2]
                co = None
                if ctarget > 0 or (cam_gain > 0 and np is not None):
                    try:
                        co = blend_block(buf, n, (cl, cr), cam_sub[0], cam_sub[1], cam_gain, ctarget)
                    except Exception as e:   # plain pair below; the cam side never takes listen down
                        err.write(f'C blend failed: {e}\n'); err.flush()
                        co, ctarget = None, 0.0
                        cam = (cl, cr, dframes, cam[3], None)    # don't retry every 10 ms
                cam_gain = ctarget
                if co is None:
                    co = bytearray(n * 6)
                    lo2, ro2 = cl * 3, cr * 3
                    for k in range(3):
                        co[k::6] = buf[lo2 + k:n * FRAME:FRAME]
                        co[3 + k::6] = buf[ro2 + k:n * FRAME:FRAME]
                cam_out.put(cam_delay.process(co, dframes * 6) if dframes or cam_delay.len else bytes(co))
            except Exception as e:
                err.write(f'C {e}\n'); err.flush()
                cam = None
        now = time.time()
        if now - last >= 0.1:
            last = now
            try:
                m = os.stat(ctl).st_mtime_ns
                if m != mtime:
                    mtime = m; pair, blend = read_route(ctl, (pair, blend))
            except OSError:
                pass
            if cam_ctl:
                cam_poll()
            err.write(f'S {peak_db(st, 0):.1f} {peak_db(st, 3):.1f} {overruns[0]}\n'); err.flush()


if __name__ == '__main__':
    main()
