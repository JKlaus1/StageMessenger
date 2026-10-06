"""
Channel-pair picker -- runs as its OWN process so the web server's GIL can never stall
audio capture (v1 sliced inside the Flask process; heavy fader traffic caused dropouts).

  usage: picker.py <ctl_file> <capture cmd ...>

stdin : unused
stdout: raw stereo s24le 48 kHz (piped straight into ffmpeg by the parent)
stderr: one status line per ~100 ms ->  "S <peakL_dB> <peakR_dB> <overruns>"
        on capture failure           ->  "E <message>"   then exit 1
ctl   : text file "L R" (zero-based USB channel indices); re-read when its mtime changes.

Cam output (v3.4, optional): when the PICKER_CAM_CTL environment variable names a file, that file may
hold one line  "L R delay_ms /path/to/fifo"  (or be missing / say "off"). While it is set the picker
also slices a SECOND stereo pair out of the same capture, delays it by delay_ms (sample-accurate, live
adjustable) and writes it to the fifo for the video encoder. The listen-back path above is written
FIRST and is never waited on: the cam side has its own bounded queue and writer thread and drops
audio when its reader stalls, so a stuck video encoder can't add a microsecond to the listen feed.
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

CHANNELS = 48
FRAME = CHANNELS * 3
CHUNK = FRAME * 480                     # 10 ms
FULL = 8388608.0


def read_pair(path, cur):
    try:
        l, r = open(path).read().split()[:2]
        return max(0, min(CHANNELS - 1, int(l))), max(0, min(CHANNELS - 1, int(r)))
    except Exception:
        return cur


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
    """-> (L, R, delay_frames, fifo) or None when the cam output is off."""
    try:
        parts = open(path).read().split(None, 3)
    except OSError:
        return None
    if len(parts) < 4 or parts[0] == 'off':
        return None
    try:
        l, r = int(parts[0]), int(parts[1])
        ms = max(0, min(MAX_DELAY_MS, int(float(parts[2]))))
    except ValueError:
        return None
    fifo = parts[3].strip()
    if not fifo:
        return None
    return (max(0, min(CHANNELS - 1, l)), max(0, min(CHANNELS - 1, r)), ms * 48, fifo)


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
    pair = read_pair(ctl, (0, 1))
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
        lo, ro = pair[0] * 3, pair[1] * 3
        st = bytearray(n * 6)
        for k in range(3):               # C-speed strided copies, no per-sample Python
            st[k::6] = buf[lo + k:n * FRAME:FRAME]
            st[3 + k::6] = buf[ro + k:n * FRAME:FRAME]
        try:
            out.write(st); out.flush()
        except (BrokenPipeError, OSError):
            cap.kill(); sys.exit(0)
        if cam is not None and cam_out is not None:      # after the listen write: listen never waits on this
            try:
                cl, cr, dframes, _ = cam
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
                    mtime = m; pair = read_pair(ctl, pair)
            except OSError:
                pass
            if cam_ctl:
                cam_poll()
            err.write(f'S {peak_db(st, 0):.1f} {peak_db(st, 3):.1f} {overruns[0]}\n'); err.flush()


if __name__ == '__main__':
    main()
