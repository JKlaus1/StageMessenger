"""
Listen-back: WING USB capture (48 ch, S24_3LE, 48 kHz) -> pick one channel pair ->
ffmpeg MP3 -> fan out to HTTP listeners.

Pipeline (v1.1):  arecord -> picker.py (own process) -> ffmpeg -> this process (fan-out)
The audio-rate work happens outside the web server, so fader traffic can't stall it;
this process only handles the ~16 KB/s MP3 output.

Capture runs only while at least one listener is connected (10 s grace after the last
one leaves). Switching feeds rewrites a small control file the picker watches, so the
MP3 stream never restarts. New listeners get a short cushion of recent MP3 up front.

Latency (v1.10): the cushion is cushion_s (default 0.5 s, was 1.5 s); each listener's queue
is capped at max_queue_s -- a listener that falls behind is flushed back to live instead of
hearing everything late. Every listener has an id and a start offset in the encoded stream,
so position(id) lets the page measure exactly how far behind live it is playing.

Low-latency (v2.0): when rtc_url is set, the same ffmpeg also encodes Opus and publishes it to
MediaMTX over RTSP (ffmpeg tee muxer, onfail=ignore -- if MediaMTX is down the MP3 side carries
on). WebRTC listeners live in MediaMTX, so rtc_probe() (the MediaMTX API) tells the watchdog how
many there are: capture keeps running while anyone listens either way. hold_rtc() starts capture
ahead of a WebRTC handshake, because MediaMTX refuses a reader until the stream is publishing.

Cam feed (v3.4): the video feed's audio is a second pair sliced out of this same capture (arecord on
hw:WING allows one opener). picker.py does that when cam_ctl names its control file; cam.py owns that
file and tells this class, through `keepalive`, that capture must keep running while video is active.
Nothing on the listen path changes.

Console-generic capture + sub blend (v3.9): the capture device and its channel count come from the
console (WING USB hw:WING 48 ch; X32 X-LIVE USB hw:XLIVE 32 ch -- both S24_3LE 48 kHz). set_capture()
swaps them (a running pipeline is respawned; MP3 listeners carry on). set_route() writes the pair plus
an optional sub pair and linear gain; picker.py blends them (see its docstring).
"""
import collections
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

CHANNELS = 48
HERE = os.path.dirname(os.path.abspath(__file__))


def _bytes_per_sec(bitrate):
    try:
        return int(str(bitrate).lower().rstrip('k')) * 1000 // 8
    except ValueError:
        return 16000


class _Client:
    """One HTTP listener: its MP3 queue plus where its stream started (for lag measurement)."""
    def __init__(self, cid, start):
        self.cid, self.start = cid, start     # start = encoded byte offset of its first byte
        self.dropped = 0                      # bytes skipped by flushes (it never received them)
        self.drops = 0
        self.qbytes = 0
        self.q = queue.Queue()
        self.lock = threading.Lock()

    def put(self, b):
        with self.lock:
            if b is not None:
                self.qbytes += len(b)
            self.q.put_nowait(b)

    def get(self, timeout):
        b = self.q.get(timeout=timeout)
        if b:
            with self.lock:
                self.qbytes = max(0, self.qbytes - len(b))
        return b

    def flush(self):
        with self.lock:
            while True:
                try:
                    b = self.q.get_nowait()
                except queue.Empty:
                    break
                if b is None:                 # keep a shutdown signal
                    self.q.put_nowait(None)
                    break
                self.dropped += len(b)
            self.qbytes = 0
            self.drops += 1


def _from_frame_start(buf):
    """Trim to the first MPEG-1 Layer III frame header so a new player doesn't start mid-frame
    (v1.1: 'go live' hiccupped while the decoder hunted for sync)."""
    def hdr_len(i):
        if i + 3 > len(buf) or buf[i] != 0xFF or (buf[i + 1] & 0xFE) != 0xFA:
            return 0
        br, sr = buf[i + 2] >> 4, (buf[i + 2] >> 2) & 3
        if br in (0, 15) or sr == 3:
            return 0
        return 144000 * _BITRATES[br] // _RATES[sr] + ((buf[i + 2] >> 1) & 1)
    for i in range(min(len(buf) - 3, 4096)):
        n = hdr_len(i)
        if n and (i + n >= len(buf) or hdr_len(i + n)):     # confirm the next header lines up
            return buf[i:]
    return b''


OPUS_RATES = ['64k', '96k', '128k', '160k']      # Listen card choices for the low-latency stream (v3.5)

_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_RATES = [44100, 48000, 32000]


def capture_cmd_for(device, channels):
    return ['arecord', '-D', device, '-c', str(int(channels)),
            '-f', 'S24_3LE', '-r', '48000', '-t', 'raw', '--buffer-time=500000']


class Listener:
    def __init__(self, capture_cmd=None, bitrate='128k', on_status=None, cushion_s=0.5, max_queue_s=1.0,
                 rtc_url='', opus_bitrate='96k', rtc_probe=None, cam_ctl='', channels=CHANNELS):
        self.channels = int(channels)
        self.capture_cmd = capture_cmd or capture_cmd_for('hw:WING', self.channels)
        self.bitrate = bitrate
        self.bps = _bytes_per_sec(bitrate)
        self.cushion_bytes = int(self.bps * max(0.0, float(cushion_s)))
        self.max_queue_bytes = int(self.bps * max(0.25, float(max_queue_s)))
        self._pos = 0                        # bytes encoded since the pipeline started
        self.rtc_url = rtc_url or ''
        self.cam_ctl = cam_ctl or ''          # picker's cam control file ('' = no cam output)
        self.keepalive = lambda: False        # cam.py: True while video needs the capture running
        self.opus_bitrate = opus_bitrate
        self.rtc_probe = rtc_probe or (lambda: (False, False, 0))   # -> (api_ok, ready, readers)
        self.rtc_readers = 0
        self.rtc_ready = False
        self._hold_until = 0.0
        self._started_at = 0.0
        self._restarts = 0
        self.on_status = on_status or (lambda st: None)
        self.pair = (0, 1)
        self.blend = None                     # (sub_l, sub_r, gain_linear) or None   (v3.9)
        self.blend_info = {'on': False, 'db': 0.0}
        self.numpy_ok = True                  # cleared when picker says it can't blend
        shm = '/dev/shm' if os.path.isdir('/dev/shm') else tempfile.gettempdir()
        self.ctl = os.path.join(shm, f'wing_pair_{os.getpid()}')
        self._write_ctl()
        self.clients = set()
        self.lock = threading.Lock()
        self.running = False
        self.error = ''
        self.peak = (-120.0, -120.0)
        self.overruns = 0
        self._backlog = collections.deque()
        self._backlog_len = 0
        self._procs = []
        self._idle_since = None
        self._gen = 0

    def set_opus_bitrate(self, rate):
        """Change the low-latency stream's bitrate. A running pipeline is respawned (MP3 listeners
        carry on; WebRTC listeners reconnect -- the page does that by itself)."""
        rate = str(rate)
        if rate not in OPUS_RATES:
            return False
        if rate != self.opus_bitrate:
            self.opus_bitrate = rate
            with self.lock:
                running = self.running
            if running and self.rtc_url:
                self._restart_encoder('opus bitrate ' + rate)
            self._status()
        return True

    # ── feed selection ──
    def _write_ctl(self):
        tmp = self.ctl + '.tmp'
        line = f'{self.pair[0]} {self.pair[1]}'
        if self.blend:
            line += f' {self.blend[0]} {self.blend[1]} {self.blend[2]:.6f}'
        with open(tmp, 'w') as f:
            f.write(line + '\n')
        os.replace(tmp, self.ctl)

    def _clamp(self, v):
        return max(0, min(self.channels - 1, int(v)))

    def set_pair(self, left, right):
        self.set_route(left, right, None)

    def set_route(self, left, right, blend=None, info=None):
        """Pair plus optional sub blend (sub_l, sub_r, gain_linear). info -> status ('on', 'db')."""
        self.pair = (self._clamp(left), self._clamp(right))
        self.blend = (self._clamp(blend[0]), self._clamp(blend[1]), float(blend[2])) \
            if blend and float(blend[2]) > 0 else None
        if info is not None:
            self.blend_info = dict(info)
        self._write_ctl()
        self._status()

    def set_capture(self, cmd, channels):
        """Switch capture device / width (console swap). Respawns a running pipeline."""
        cmd, channels = list(cmd), int(channels)
        if cmd == self.capture_cmd and channels == self.channels:
            return
        self.capture_cmd, self.channels = cmd, channels
        self.pair = (self._clamp(self.pair[0]), self._clamp(self.pair[1]))
        self.blend = None
        self._write_ctl()
        with self.lock:
            running = self.running
        if running:
            self._restart_encoder('capture device ' + ' '.join(cmd[1:3]))

    # ── listener management ──
    def add_client(self, cid=''):
        with self.lock:
            cushion = _from_frame_start(b''.join(self._backlog))
            c = _Client(str(cid)[:40], self._pos - len(cushion))
            if cushion:                      # prefill the cushion, starting on an MP3 frame
                c.put(cushion)
            self.clients.add(c)
            self._idle_since = None
            need_start = not self.running
        if need_start:
            self._start()
        self._status()
        return c

    def position(self, cid):
        """Seconds: live encoder position, where this listener's stream began, what it skipped,
        and what is still queued for it on the Pi. live - start - dropped - player.currentTime = lag."""
        with self.lock:
            c = next((x for x in self.clients if x.cid and x.cid == cid), None)
            pos = self._pos
        if not c:
            return {'ok': False}
        b = float(self.bps)
        return {'ok': True, 'pos': pos / b, 'start': c.start / b, 'dropped': c.dropped / b,
                'queued': c.qbytes / b, 'drops': c.drops}

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)
            if not self.clients:
                self._idle_since = time.time()
        self._status()

    # ── WebRTC (MediaMTX) side ──
    def hold_rtc(self, seconds=20):
        """Keep capture running for `seconds` (covers a WebRTC handshake) and start it if idle."""
        self._hold_until = max(self._hold_until, time.time() + seconds)
        with self.lock:
            need_start = not self.running
        if need_start:
            self._start()

    def ensure_capture(self):
        """Start capture if idle (the video feed needs the audio pipeline up)."""
        with self.lock:
            need_start = not self.running
        if need_start:
            self._start()

    def wait_rtc_ready(self, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            api_ok, ready, readers = self.rtc_probe()
            if ready:
                self.rtc_ready = True
                return True
            time.sleep(0.2)
        return False

    def status(self):
        return {
            'running':   self.running,
            'listeners': len(self.clients),
            'rtc':       self.rtc_readers,
            'error':     self.error,
            'pair':      list(self.pair),
            'blend':     {**self.blend_info, 'active': bool(self.blend), 'numpy': self.numpy_ok},
            'device':    self.capture_cmd[2] if len(self.capture_cmd) > 2 else '',
            'peak':      [round(p, 1) for p in self.peak],
            'overruns':  self.overruns,
            'opus_bitrate': self.opus_bitrate,
            'opus_rates':   OPUS_RATES,
        }

    def _status(self):
        try:
            self.on_status(self.status())
        except Exception:
            pass

    # ── pipeline ──
    def _start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.error = ''
            self.overruns = 0
            self._backlog.clear(); self._backlog_len = 0
            self._pos = 0
            self._gen += 1
            gen = self._gen
            self._started_at = time.time()
        if self.rtc_url:          # one encoder, two outputs: MP3 for HTTP listeners + Opus for MediaMTX
            out = ['-map', '0:a', '-map', '0:a',
                   '-c:a:0', 'libmp3lame', '-b:a:0', self.bitrate, '-reservoir:a:0', '0',
                   '-c:a:1', 'libopus', '-b:a:1', self.opus_bitrate, '-application:a:1', 'lowdelay',
                   '-flush_packets', '1', '-f', 'tee',
                   '[select=0:f=mp3:onfail=ignore]pipe:1'
                   f'|[select=1:f=rtsp:rtsp_transport=tcp:onfail=ignore:use_fifo=1]{self.rtc_url}']
        else:
            out = ['-c:a', 'libmp3lame', '-b:a', self.bitrate, '-reservoir', '0',
                   '-flush_packets', '1', '-f', 'mp3', 'pipe:1']
        try:
            env = dict(os.environ)
            env.pop('PICKER_CAM_CTL', None)
            env['PICKER_CHANNELS'] = str(self.channels)
            if self.cam_ctl:
                env['PICKER_CAM_CTL'] = self.cam_ctl
            pick = subprocess.Popen([sys.executable, os.path.join(HERE, 'picker.py'), self.ctl]
                                    + self.capture_cmd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
            enc = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-f', 's24le', '-ar', '48000', '-ac', '2', '-i', 'pipe:0'] + out,
                stdin=pick.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            pick.stdout.close()                 # ffmpeg owns the pipe now
        except OSError as e:
            self.error = f'start failed: {e}'
            self._teardown(); self._status()
            return
        self._procs = [pick, enc]
        threading.Thread(target=self._status_loop, args=(gen, pick), daemon=True, name='listen-st').start()
        threading.Thread(target=self._encode_loop, args=(gen, enc), daemon=True, name='listen-enc').start()
        threading.Thread(target=self._watchdog, args=(gen,), daemon=True, name='listen-wd').start()
        self._status()

    def _teardown(self, keep_clients=False):
        with self.lock:
            self.running = False
            self._gen += 1
            procs, self._procs = self._procs, []
            clients = [] if keep_clients else list(self.clients)
        for p in procs:
            try:
                p.kill(); p.wait(timeout=2)
            except Exception:
                pass
        self.peak = (-120.0, -120.0)
        for c in clients:                       # unblock HTTP generators
            c.put(None)

    def _status_loop(self, gen, pick):
        for raw in iter(pick.stderr.readline, b''):
            if gen != self._gen:
                return
            line = raw.decode(errors='replace').strip()
            if line.startswith('S '):
                try:
                    _, l, r, ov = line.split()
                    self.peak = (float(l), float(r)); self.overruns = int(ov)
                except ValueError:
                    pass
            elif line.startswith('E '):
                self.error = line[2:][:200]
            elif line.startswith('W ') and 'numpy' in line:
                self.numpy_ok = False
                print('[mixer] listen: ' + line[2:], flush=True)
        if gen == self._gen:
            self.error = self.error or 'capture stopped'
            self._teardown(); self._status()

    def _encode_loop(self, gen, enc):
        while gen == self._gen:
            try:
                chunk = os.read(enc.stdout.fileno(), 4096)
            except Exception:
                chunk = b''
            if not chunk:
                if gen == self._gen:
                    self.error = self.error or 'encoder stopped'
                    self._teardown(); self._status()
                return
            with self.lock:
                self._pos += len(chunk)
                self._backlog.append(chunk); self._backlog_len += len(chunk)
                while self._backlog and self._backlog_len - len(self._backlog[0]) >= self.cushion_bytes:
                    self._backlog_len -= len(self._backlog.popleft())
                clients = list(self.clients)
            for c in clients:
                if c.qbytes + len(chunk) > self.max_queue_bytes:
                    # Fallen behind (slow link): skip to live rather than play everything late.
                    c.flush()
                    head = _from_frame_start(chunk)
                    with c.lock:
                        c.dropped += len(chunk) - len(head)
                    if head:
                        c.put(head)
                else:
                    c.put(chunk)

    def _restart_encoder(self, why):
        """Respawn capture+encoder without dropping MP3 listeners (their HTTP streams just continue;
        MP3 decoders don't mind a new encoder). Used when MediaMTX lost the Opus publisher."""
        self._restarts += 1
        print(f'[mixer] listen: restarting encoder ({why})', flush=True)
        self._teardown(keep_clients=True)
        self._start()

    def _watchdog(self, gen):
        last_probe, not_ready_since, idle_since = 0.0, None, None
        while gen == self._gen:
            time.sleep(0.25)
            now = time.time()
            if self.rtc_url and now - last_probe >= 2:
                last_probe = now
                api_ok, ready, readers = self.rtc_probe()
                self.rtc_readers, self.rtc_ready = readers, ready
                # MediaMTX is up but our Opus publisher isn't there (MediaMTX restarted, or it was
                # down when capture started): reconnect -- but only once MediaMTX itself answers.
                if api_ok and not ready and now - self._started_at > 6:
                    not_ready_since = not_ready_since or now
                    if now - not_ready_since > 4:
                        self._restart_encoder('MediaMTX has no stream')
                        return
                else:
                    not_ready_since = None
            with self.lock:
                busy = bool(self.clients)
            try:
                cam_busy = bool(self.keepalive())
            except Exception:
                cam_busy = False
            if busy or cam_busy or self.rtc_readers > 0 or now < self._hold_until:
                idle_since = None
            else:
                idle_since = idle_since or now
                if now - idle_since > 10:
                    self._teardown(); self._status()
                    return
            self._status()                      # carries the peak meter (4 Hz)
