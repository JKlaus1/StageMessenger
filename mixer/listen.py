"""
Listen-back: WING USB capture (48 ch, S24_3LE, 48 kHz) -> pick one channel pair ->
ffmpeg MP3 -> fan out to HTTP listeners.

Capture runs only while at least one listener is connected (10 s grace after the last
one leaves). Switching feeds only changes which bytes are sliced out of each frame, so
the MP3 stream never restarts and listeners don't reconnect.
"""
import os
import queue
import subprocess
import threading
import time

CHANNELS = 48
BYTES_PER_SAMPLE = 3
FRAME = CHANNELS * BYTES_PER_SAMPLE          # 144 bytes per 48-channel frame
CHUNK_FRAMES = 480                            # 10 ms at 48 kHz
FULL_SCALE = 8388608.0


class Listener:
    def __init__(self, capture_cmd=None, bitrate='128k', on_status=None):
        self.capture_cmd = capture_cmd or [
            'arecord', '-q', '-D', 'hw:WING', '-c', str(CHANNELS),
            '-f', 'S24_3LE', '-r', '48000', '-t', 'raw', '--buffer-time=200000',
        ]
        self.bitrate = bitrate
        self.on_status = on_status or (lambda st: None)
        self.pair = (0, 1)                    # zero-based USB channel indices (L, R)
        self.clients = set()
        self.lock = threading.Lock()
        self.running = False
        self.error = ''
        self.peak = (-120.0, -120.0)
        self._cap = None
        self._enc = None
        self._idle_since = None
        self._gen = 0                         # bumps per start; stale threads exit

    # ── feed selection ──
    def set_pair(self, left, right):
        self.pair = (max(0, min(CHANNELS - 1, int(left))), max(0, min(CHANNELS - 1, int(right))))

    # ── listener management ──
    def add_client(self):
        q = queue.Queue(maxsize=400)
        with self.lock:
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
            self._gen += 1
            gen = self._gen
        try:
            self._cap = subprocess.Popen(self.capture_cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0)
            self._enc = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-f', 's24le', '-ar', '48000', '-ac', '2', '-i', 'pipe:0',
                 '-c:a', 'libmp3lame', '-b:a', self.bitrate, '-reservoir', '0',
                 '-flush_packets', '1', '-f', 'mp3', 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            self.error = f'start failed: {e}'
            self._teardown()
            self._status()
            return
        threading.Thread(target=self._capture_loop, args=(gen,), daemon=True, name='listen-cap').start()
        threading.Thread(target=self._encode_loop, args=(gen,), daemon=True, name='listen-enc').start()
        threading.Thread(target=self._watchdog, args=(gen,), daemon=True, name='listen-wd').start()
        self._status()

    def _teardown(self):
        with self.lock:
            self.running = False
            self._gen += 1
            cap, enc = self._cap, self._enc
            self._cap = self._enc = None
        for p in (cap, enc):
            if p:
                try:
                    p.kill(); p.wait(timeout=2)
                except Exception:
                    pass
        self.peak = (-120.0, -120.0)
        with self.lock:
            clients = list(self.clients)
        for q in clients:                     # unblock HTTP generators
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def _capture_loop(self, gen):
        cap, enc = self._cap, self._enc
        want = FRAME * CHUNK_FRAMES
        rest = b''
        last_meter = 0.0
        while gen == self._gen:
            try:
                data = cap.stdout.read(want)
            except Exception:
                data = b''
            if not data:
                err = ''
                try:
                    err = cap.stderr.read().decode(errors='replace').strip()
                except Exception:
                    pass
                if gen == self._gen:
                    self.error = 'capture stopped' + (f': {err[:160]}' if err else '')
                    self._teardown()
                    self._status()
                return
            buf = rest + data
            n = len(buf) // FRAME
            rest = buf[n * FRAME:]
            buf = buf[:n * FRAME]
            lo = self.pair[0] * BYTES_PER_SAMPLE
            ro = self.pair[1] * BYTES_PER_SAMPLE
            out = bytearray(n * 6)
            for k in range(3):                # C-speed strided copies: no per-sample Python
                out[k::6] = buf[lo + k::FRAME]
                out[3 + k::6] = buf[ro + k::FRAME]
            try:
                enc.stdin.write(out)
            except Exception:
                return
            now = time.time()
            if now - last_meter > 0.1:
                last_meter = now
                self.peak = (self._peak(out, 0), self._peak(out, 3))

    @staticmethod
    def _peak(stereo, off):
        pk = 0
        for i in range(off, len(stereo) - 2, 6 * 8):    # every 8th frame is plenty for a meter
            v = abs(int.from_bytes(stereo[i:i + 3], 'little', signed=True))
            if v > pk:
                pk = v
        if pk == 0:
            return -120.0
        import math
        return max(-120.0, 20 * math.log10(pk / FULL_SCALE))

    def _encode_loop(self, gen):
        enc = self._enc
        while gen == self._gen:
            try:
                chunk = os.read(enc.stdout.fileno(), 4096)
            except Exception:
                chunk = b''
            if not chunk:
                if gen == self._gen:
                    self.error = self.error or 'encoder stopped'
                    self._teardown()
                    self._status()
                return
            with self.lock:
                clients = list(self.clients)
            for q in clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:            # slow client: drop; mp3 decoders resync
                    pass

    def _watchdog(self, gen):
        last_push = 0.0
        while gen == self._gen:
            time.sleep(0.25)
            with self.lock:
                idle = self._idle_since
            if idle and time.time() - idle > 10:
                self._teardown()
                self._status()
                return
            if time.time() - last_push > 0.25:
                last_push = time.time()
                self._status()                # carries the peak meter
