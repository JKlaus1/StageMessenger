"""
Listen-back: WING USB capture (48 ch, S24_3LE, 48 kHz) -> pick one channel pair ->
ffmpeg MP3 -> fan out to HTTP listeners.

Pipeline (v1.1):  arecord -> picker.py (own process) -> ffmpeg -> this process (fan-out)
The audio-rate work happens outside the web server, so fader traffic can't stall it;
this process only handles the ~16 KB/s MP3 output.

Capture runs only while at least one listener is connected (10 s grace after the last
one leaves). Switching feeds rewrites a small control file the picker watches, so the
MP3 stream never restarts. New listeners get the last ~1.5 s of MP3 up front: that
cushion absorbs the drift between the WING's clock and the phone's playback clock.
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
BACKLOG_BYTES = 24 * 1024            # ~1.5 s at 128 kb/s


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


_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_RATES = [44100, 48000, 32000]


class Listener:
    def __init__(self, capture_cmd=None, bitrate='128k', on_status=None):
        self.capture_cmd = capture_cmd or [
            'arecord', '-D', 'hw:WING', '-c', str(CHANNELS),
            '-f', 'S24_3LE', '-r', '48000', '-t', 'raw', '--buffer-time=500000',
        ]
        self.bitrate = bitrate
        self.on_status = on_status or (lambda st: None)
        self.pair = (0, 1)
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

    # ── feed selection ──
    def _write_ctl(self):
        tmp = self.ctl + '.tmp'
        with open(tmp, 'w') as f:
            f.write(f'{self.pair[0]} {self.pair[1]}\n')
        os.replace(tmp, self.ctl)

    def set_pair(self, left, right):
        self.pair = (max(0, min(CHANNELS - 1, int(left))), max(0, min(CHANNELS - 1, int(right))))
        self._write_ctl()

    # ── listener management ──
    def add_client(self):
        q = queue.Queue(maxsize=400)
        with self.lock:
            cushion = _from_frame_start(b''.join(self._backlog))
            if cushion:                      # prefill the cushion, starting on an MP3 frame
                q.put_nowait(cushion)
            self.clients.add(q)
            self._idle_since = None
            need_start = not self.running
        if need_start:
            self._start()
        self._status()
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)
            if not self.clients:
                self._idle_since = time.time()
        self._status()

    def status(self):
        return {
            'running':   self.running,
            'listeners': len(self.clients),
            'error':     self.error,
            'pair':      list(self.pair),
            'peak':      [round(p, 1) for p in self.peak],
            'overruns':  self.overruns,
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
            self._gen += 1
            gen = self._gen
        try:
            pick = subprocess.Popen([sys.executable, os.path.join(HERE, 'picker.py'), self.ctl]
                                    + self.capture_cmd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            enc = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-f', 's24le', '-ar', '48000', '-ac', '2', '-i', 'pipe:0',
                 '-c:a', 'libmp3lame', '-b:a', self.bitrate, '-reservoir', '0',
                 '-flush_packets', '1', '-f', 'mp3', 'pipe:1'],
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

    def _teardown(self):
        with self.lock:
            self.running = False
            self._gen += 1
            procs, self._procs = self._procs, []
            clients = list(self.clients)
        for p in procs:
            try:
                p.kill(); p.wait(timeout=2)
            except Exception:
                pass
        self.peak = (-120.0, -120.0)
        for q in clients:                       # unblock HTTP generators
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

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
                self._backlog.append(chunk); self._backlog_len += len(chunk)
                while self._backlog_len > BACKLOG_BYTES:
                    self._backlog_len -= len(self._backlog.popleft())
                clients = list(self.clients)
            for q in clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:              # slow client: drop; mp3 decoders resync
                    pass

    def _watchdog(self, gen):
        while gen == self._gen:
            time.sleep(0.25)
            with self.lock:
                idle = self._idle_since
            if idle and time.time() - idle > 10:
                self._teardown(); self._status()
                return
            self._status()                      # carries the peak meter (4 Hz)
