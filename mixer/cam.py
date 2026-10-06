"""
Video feed for /mixer (v3.4): a USB webcam + a console feed (Main LR by default) published to
MediaMTX as one WebRTC stream ("cam" path), separate from the low-latency listen-back.

    webcam (MJPEG) ---------------------------------> ffmpeg (x264 + Opus) -> RTSP -> MediaMTX -> WHEP
    hw:WING -> picker.py (2nd pair, delayed) -> fifo ->/

Why it is built this way
  * One capture. `arecord -D hw:WING` allows a single opener, so the audio for the video is a second
    pair sliced out of the capture the listen-back already runs (picker.py, PICKER_CAM_CTL). The listen
    path is written first and never waits on this one; this module only writes the picker's cam
    control file and runs ffmpeg.
  * A/V alignment. The camera + x264 pipeline is later than the console audio, and video can't be made
    earlier, so the AUDIO is delayed (cam delay_ms, live adjustable, done in the picker with a
    sample-accurate delay line). Both ffmpeg inputs are stamped with the wall clock and `-copyts
    -start_at_zero` keeps their true offset, so the setting survives encoder restarts.
  * Easy on the Pi. ffmpeg runs niced, on 2 x264 threads, 720p30 @ 1.5 Mbps by default, only while
    someone is watching (idle_s after the last viewer), and starts the audio pipeline through
    Listener.ensure_capture() / keepalive() without touching it otherwise.
  * Ready for a later YouTube push: constrained-baseline H.264, 2 s keyframes, CBR-ish -- a second
    ffmpeg can read rtsp://127.0.0.1:8554/cam, copy the video and make AAC.
"""
import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time

MAX_DELAY_MS = 3000

QUALITIES = {
    '720p30': {'label': '720p · 30 fps', 'w': 1280, 'h': 720, 'fps': 30, 'bitrate': '1500k'},
    '720p20': {'label': '720p · 20 fps', 'w': 1280, 'h': 720, 'fps': 20, 'bitrate': '1000k'},
    '480p30': {'label': '480p · 30 fps', 'w': 640, 'h': 480, 'fps': 30, 'bitrate': '700k'},
}
DEFAULT_QUALITY = '720p30'


def find_device(configured=''):
    """The webcam's capture node: the configured path, else the first USB camera by stable id
    (/dev/video numbers shift around the Pi 5's other video nodes), else ''."""
    if configured:
        return configured if os.path.exists(configured) else ''
    found = sorted(glob.glob('/dev/v4l/by-id/*-video-index0'))
    return found[0] if found else ''


class Cam:
    def __init__(self, cfg, listener, feeds, probe, ctl_path, on_status=None, save=None,
                 out_url='rtsp://127.0.0.1:8554/cam', video_input=None, output=None):
        self.cfg = dict(cfg or {})
        self.enabled = bool(self.cfg.get('enabled', True))
        self.listener = listener
        self.feeds = feeds                        # () -> [{'id', 'usb': [l, r]} ...] (1-based); [] = no console audio
        self.probe = probe                        # () -> (api_ok, ready, readers) for the MediaMTX 'cam' path
        self.ctl = ctl_path                       # the picker's cam control file
        self.on_status = on_status or (lambda st: None)
        self.save = save or (lambda d: None)
        self.out_url = out_url
        self._video_input = video_input           # tests: replaces the v4l2 input
        self._output = output                     # tests: replaces the RTSP output args
        q = self.cfg.get('quality', DEFAULT_QUALITY)
        self.quality = q if q in QUALITIES else DEFAULT_QUALITY
        self.feed = str(self.cfg.get('feed') or 'main1')
        self.delay_ms = self._clamp_delay(self.cfg.get('delay_ms', 0))
        self.idle_s = float(self.cfg.get('idle_s', 15))
        self.lock = threading.RLock()
        self.running = False
        self.audio = False
        self.error = ''
        self.restarts = 0
        self.readers = 0
        self.ready = False
        self._proc = None
        self._holder = -1
        self._fifo = ''
        self._gen = 0
        self._hold_until = 0.0
        self._started_at = 0.0
        self._fails = []
        self._tail = []
        self._last_pub = None
        self._no_audio_until = 0.0              # capture failed: run picture-only until then
        self._audio_issue = ''
        shm = '/dev/shm' if os.path.isdir('/dev/shm') else tempfile.gettempdir()
        self._fifo_base = os.path.join(shm, f'stage_cam_{os.getpid()}')
        self.listener.keepalive = self.keepalive
        self._write_ctl(off=True)

    # ── settings ──
    @staticmethod
    def _clamp_delay(v):
        try:
            return max(0, min(MAX_DELAY_MS, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    def _pair(self):
        for f in self.feeds() or []:
            if f['id'] == self.feed:
                return f['usb'][0] - 1, f['usb'][1] - 1
        return None

    def set_feed(self, fid):
        for f in self.feeds() or []:
            if f['id'] == fid:
                self.feed = fid
                self._write_ctl()
                self._saved(); self._publish()
                if self.running and not self.audio and self._pair():     # picture-only after a capture failure: try audio again
                    self._no_audio_until = 0.0
                    self._restart('feed chosen again')
                return True
        return False

    def set_delay(self, ms):
        self.delay_ms = self._clamp_delay(ms)
        self._write_ctl()
        self._saved(); self._publish()
        return self.delay_ms

    def set_quality(self, name):
        if name not in QUALITIES:
            return False
        changed = name != self.quality
        self.quality = name
        self._saved()
        if changed and self.running:
            self._restart('quality change')
        self._publish()
        return True

    def _saved(self):
        try:
            self.save({'feed': self.feed, 'delay_ms': self.delay_ms, 'quality': self.quality})
        except Exception as e:
            print(f'[mixer] cam: could not save settings: {e}', flush=True)

    # ── state ──
    def device(self):
        return find_device(self.cfg.get('device', ''))

    def available(self):
        return self.enabled and bool(self._video_input or self.device())

    def keepalive(self):
        """Listener watchdog: capture must keep running while the video carries console audio."""
        return self.running and self.audio

    def status(self):
        return {
            'enabled':   self.enabled,
            'available': self.available(),
            'running':   self.running,
            'viewers':   self.readers,
            'audio':     self.audio if self.running else bool(self._pair()),
            'feed':      self.feed,
            'delay_ms':  self.delay_ms,
            'quality':   self.quality,
            'qualities': [{'id': k, 'label': v['label']} for k, v in QUALITIES.items()],
            'max_delay': MAX_DELAY_MS,
            'error':     self.error,
            'audio_issue': self._audio_issue if self.running and not self.audio else '',
            'restarts':  self.restarts,
        }

    def _publish(self):
        st = self.status()
        if st != self._last_pub:
            self._last_pub = st
            try:
                self.on_status(st)
            except Exception:
                pass

    def _write_ctl(self, off=False):
        """The picker's cam line: 'L R delay_ms fifo' while running with audio, else 'off'."""
        pair = None if off or not (self.running and self.audio and self._fifo) else self._pair()
        line = 'off\n' if pair is None else f'{pair[0]} {pair[1]} {self.delay_ms} {self._fifo}\n'
        try:
            tmp = self.ctl + '.tmp'
            with open(tmp, 'w') as f:
                f.write(line)
            os.replace(tmp, self.ctl)
        except OSError as e:
            print(f'[mixer] cam: could not write {self.ctl}: {e}', flush=True)

    # ── viewers ──
    def hold(self, seconds=20):
        """A viewer is connecting: make sure the encoder is up and keep it up through the handshake."""
        if not self.available():
            return False
        self._hold_until = max(self._hold_until, time.time() + seconds)
        with self.lock:
            need = not self.running
        if need:
            self._start()
        return True

    def wait_ready(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if not self.running and self.error:
                return False
            api_ok, ready, readers = self.probe()
            if ready:
                self.ready = True
                return True
            time.sleep(0.25)
        return False

    def console_changed(self):
        """The console was swapped (WING <-> X32): audio may have appeared or gone."""
        self._no_audio_until = 0.0
        if self.running:
            self._restart('console changed')

    # ── pipeline ──
    def _cmd(self, audio):
        q = QUALITIES[self.quality]
        fps = q['fps']
        kbps = int(q['bitrate'].rstrip('k'))
        c = []
        nice = int(self.cfg.get('nice', 15))
        if nice and shutil.which('nice'):
            c += ['nice', '-n', str(nice)]
        c += ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error']
        if self._video_input:
            c += list(self._video_input)
        else:
            c += ['-thread_queue_size', '512', '-f', 'v4l2', '-input_format', 'mjpeg',
                  '-video_size', f"{q['w']}x{q['h']}", '-framerate', str(fps),
                  '-use_wallclock_as_timestamps', '1', '-i', self.device()]
        if audio:
            c += ['-thread_queue_size', '1024', '-f', 's24le', '-ar', '48000', '-ac', '2',
                  '-use_wallclock_as_timestamps', '1', '-i', self._fifo]
        c += ['-copyts', '-start_at_zero']
        c += ['-map', '0:v:0', '-vf', f'fps={fps}',
              '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
              '-profile:v', 'baseline', '-pix_fmt', 'yuv420p',
              '-b:v', q['bitrate'], '-maxrate', q['bitrate'], '-bufsize', f'{2 * kbps}k',
              '-g', str(int(fps * float(self.cfg.get('gop_s', 2)))), '-x264-params', 'scenecut=0',
              '-threads', str(int(self.cfg.get('threads', 2)))]
        if audio:
            c += ['-map', '1:a:0', '-af', 'aresample=async=1000',
                  '-c:a', 'libopus', '-b:a', str(self.cfg.get('audio_bitrate', '96k')),
                  '-application', 'audio', '-ar', '48000']
        else:
            c += ['-an']
        c += ['-flush_packets', '1']
        c += list(self._output) if self._output else ['-f', 'rtsp', '-rtsp_transport', 'tcp', self.out_url]
        return c

    def _start(self):
        with self.lock:
            if self.running:
                return
            if not self.available():
                self.error = 'no camera found'
                self._publish()
                return
            self._gen += 1
            gen = self._gen
            want_audio = self._pair() is not None
            audio = want_audio and time.time() >= self._no_audio_until
            if audio or not want_audio:
                self._audio_issue = ''
            self.audio, self.error, self.ready, self.readers = audio, '', False, 0
            self._tail = []
            self._started_at = time.time()
            try:
                if audio:
                    self._fifo = f'{self._fifo_base}_{gen}.pcm'      # fresh fifo per run: no stale audio
                    try:
                        os.remove(self._fifo)
                    except OSError:
                        pass
                    os.mkfifo(self._fifo, 0o600)
                    self._holder = os.open(self._fifo, os.O_RDWR | os.O_NONBLOCK)   # keeps a reader present
                self._proc = subprocess.Popen(self._cmd(audio), stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as e:
                self.error = f'start failed: {e}'
                self._cleanup()
                self._publish()
                return
            self.running = True
            proc = self._proc
        if audio:
            self.listener.ensure_capture()                     # the audio pipeline must be up (outside our lock)
        threading.Thread(target=self._drain, args=(gen, proc), daemon=True, name='cam-err').start()
        threading.Thread(target=self._supervise, args=(gen, proc, audio), daemon=True, name='cam-sup').start()
        self._publish()

    def _drain(self, gen, proc):
        for raw in iter(proc.stderr.readline, b''):
            t = raw.decode(errors='replace').strip()
            if t:
                self._tail.append(t); del self._tail[:-4]

    def _cleanup(self):
        """Release the fifo, holder and ctl (no process handling). Caller holds the lock."""
        if self._holder >= 0:
            try:
                os.close(self._holder)
            except OSError:
                pass
            self._holder = -1
        if self._fifo:
            try:
                os.remove(self._fifo)
            except OSError:
                pass
        self._fifo = ''
        self.running = False
        self._write_ctl(off=True)

    def _stop(self, why=''):
        with self.lock:
            self._gen += 1
            proc, self._proc = self._proc, None
            self._cleanup()
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=2)
            except Exception:
                pass
        self.readers, self.ready = 0, False
        if why:
            print(f'[mixer] cam: stopped ({why})', flush=True)
        self._publish()

    def _restart(self, why):
        self.restarts += 1
        print(f'[mixer] cam: restarting ({why})', flush=True)
        self._stop()
        self._start()

    def _supervise(self, gen, proc, audio):
        # Audio starts flowing only once ffmpeg has the fifo open (otherwise the first ~0.2 s of the
        # pipe's backlog would play late): wait for it, then switch the picker's cam output on.
        time.sleep(0.4)
        if gen == self._gen and proc.poll() is None:
            self._write_ctl()
            self._publish()
        last_probe, not_ready_since, idle_since = 0.0, None, None
        audio_bad, retried, last_err = None, False, ''
        while gen == self._gen:
            time.sleep(0.5)
            now = time.time()
            # ffmpeg won't publish until the audio input delivers, so a console that is not there (no
            # WING on USB yet, capture failing) must not hold the picture hostage: retry the capture
            # once, then carry on picture-only and say why.
            if audio:
                if self.listener.running:
                    audio_bad, retried = None, False
                else:
                    audio_bad = audio_bad or now
                    last_err = self.listener.error or last_err
                    if now - audio_bad > 2.5:
                        self._audio_issue = (last_err or 'audio capture is not running')[:120]
                        self._no_audio_until = now + 20
                        self._restart('console audio unavailable: ' + self._audio_issue)
                        return
                    if now - audio_bad > 1.0 and not retried:
                        retried = True
                        self.listener.ensure_capture()
            if proc.poll() is not None:                       # ffmpeg exited by itself
                if gen != self._gen:
                    return
                why = ' | '.join(self._tail[-2:]) or f'ffmpeg exited ({proc.returncode})'
                self.error = why[:200]
                self._fails = [t for t in self._fails if now - t < 60] + [now]
                if len(self._fails) > 4:                       # not going to recover on its own
                    self._stop('camera keeps failing: ' + self.error)
                    return
                self._restart('ffmpeg exited: ' + self.error)
                return
            if now - last_probe >= 2:
                last_probe = now
                api_ok, ready, readers = self.probe()
                self.readers, self.ready = readers, ready
                if api_ok and not ready and now - self._started_at > 10:
                    not_ready_since = not_ready_since or now
                    if now - not_ready_since > 5:
                        self._restart('MediaMTX has no cam stream')
                        return
                else:
                    not_ready_since = None
                if ready and self.error:
                    self.error = ''
            if self.readers > 0 or now < self._hold_until:
                idle_since = None
            else:
                idle_since = idle_since or now
                if now - idle_since > self.idle_s:
                    self._stop('no viewers')
                    return
            self._publish()
