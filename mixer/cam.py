"""
Video feed for /mixer (v3.4, tiers + audio bitrate v3.5): a USB webcam + a console feed (Main LR by default) published to
MediaMTX as one WebRTC stream ("cam" path), separate from the low-latency listen-back.

    webcam (MJPEG) ---------------------------------> ffmpeg (x264 + Opus) -> RTSP -> MediaMTX -> WHEP
    hw:WING -> picker.py (2nd pair, delayed) -> fifo ->/

Cameras (v3.8): every USB webcam is found by itself (the first one is the default); Wi-Fi cameras are
added on the page (or found with "Find cameras": IP Webcam / DroidCam on the Pi's networks) and kept in
mixer_state.json. A background check marks each Wi-Fi camera online / offline every few seconds; the
chosen camera is used while it is reachable, otherwise the Pi falls back to a USB webcam and goes back
by itself when the chosen one returns. Only the camera being watched is encoded, so the list can be long.
Per camera: rotation 0/90/180/270 (done before encoding, so it is right everywhere) or, for IP Webcam,
"auto" from the phone's accelerometer. Sound can also come from the camera's own mic (USB webcam mic =
its own ALSA card, IP Webcam = /audio.wav) instead of a console feed.

Network camera (v3.6): set cam.video_url in mixer_config.json (http(s):// MJPEG or rtsp://, e.g. an
Android phone running IP Webcam / DroidCam on the same Wi-Fi) and that replaces the USB webcam as the
picture source. The Pi PULLS the stream, so no inbound port is opened. The phone chooses its own
resolution: the picture is scaled / padded to the chosen tier's size (720p on the phone is cheapest).
The URL (it may carry a user:password) is config-only -- never exposed in status, logs or API.

Why it is built this way
  * One capture. `arecord -D hw:WING` allows a single opener, so the audio for the video is a second
    pair sliced out of the capture the listen-back already runs (picker.py, PICKER_CAM_CTL). The listen
    path is written first and never waits on this one; this module only writes the picker's cam
    control file and runs ffmpeg.
  * A/V alignment. The camera + x264 pipeline is later than the console audio, and video can't be made
    earlier, so the AUDIO is delayed (cam delay_ms, live adjustable, done in the picker with a
    sample-accurate delay line). Both ffmpeg inputs are stamped with the wall clock and `-copyts
    -start_at_zero` keeps their true offset, so the setting survives encoder restarts.
  * Easy on the Pi. ffmpeg runs niced, on 2 x264 threads, 720p30 @ 2.5 Mbps by default, only while
    someone is watching (idle_s after the last viewer), and starts the audio pipeline through
    Listener.ensure_capture() / keepalive() without touching it otherwise.
  * Ready for a later YouTube push: constrained-baseline H.264, 2 s keyframes, CBR-ish -- a second
    ffmpeg can read rtsp://127.0.0.1:8554/cam, copy the video and make AAC.
"""
import base64
import glob
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MAX_DELAY_MS = 3000

# Picture tiers (v3.5). The camera always delivers 1280x720 MJPEG (cap = its frame rate); lower tiers
# drop frames first and scale after, and spend the time saved on a slower, more efficient x264 preset
# so a low bitrate still looks like a picture instead of a smear. Every tier is 16:9.
QUALITIES = {
    'high':   {'label': 'High · 720p 30 fps · 3.5 Mbps', 'w': 1280, 'h': 720, 'fps': 30, 'cap': 30,
               'bitrate': '3500k', 'preset': 'ultrafast'},
    'good':   {'label': 'Good · 720p 30 fps · 2.5 Mbps', 'w': 1280, 'h': 720, 'fps': 30, 'cap': 30,
               'bitrate': '2500k', 'preset': 'ultrafast'},
    'medium': {'label': 'Medium · 720p 15 fps · 1.2 Mbps', 'w': 1280, 'h': 720, 'fps': 15, 'cap': 30,
               'bitrate': '1200k', 'preset': 'superfast'},
    'low':    {'label': 'Low · 480p 10 fps · 500 kbps', 'w': 854, 'h': 480, 'fps': 10, 'cap': 20,
               'bitrate': '500k', 'preset': 'veryfast'},
    'min':    {'label': 'Minimum · 360p 5 fps · 250 kbps', 'w': 640, 'h': 360, 'fps': 5, 'cap': 20,
               'bitrate': '250k', 'preset': 'veryfast'},
}
DEFAULT_QUALITY = 'good'
# Opus bitrates offered for the sound that goes with the picture (independent of the picture tier).
AUDIO_RATES = ['64k', '96k', '128k', '160k']
DEFAULT_AUDIO_RATE = '128k'

NET_SCHEMES = ('http://', 'https://', 'rtsp://')     # nothing else reaches ffmpeg (file:, concat:, ...)
NET_TIMEOUT_US = 5_000_000                           # a stalled network read gives up after 5 s
_CRED = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@')


def clean_url(u):
    """The configured network camera URL if it is an allowed scheme, else ''."""
    u = str(u or '').strip()
    return u if u.lower().startswith(NET_SCHEMES) else ''


def redact(text):
    """ffmpeg echoes the input URL in errors; keep any user:password out of logs and the page."""
    return _CRED.sub(r'\1***@', text)


ROTATIONS = ['0', '90', '180', '270']
ROT_MODES = ROTATIONS + ['auto']
MAX_NET_CAMS = 12
# IP Webcam's own stream orientation setting -> how far its picture is already turned
_IPW_BASE = {'landscape': 0, 'portrait': 90, 'upsidedown': 180, 'upsidedown_portrait': 270}


def usb_cameras(configured=''):
    """Capture nodes of every USB webcam (stable /dev/v4l/by-id names), or just the configured one."""
    if configured:
        return [configured] if os.path.exists(configured) else []
    return sorted(glob.glob('/dev/v4l/by-id/*-video-index0'))


def usb_name(path):
    """'usb-Nexight_Inc_NexiGo_N930E_FHD_Webcam_AN2023...-video-index0' -> 'NexiGo N930E FHD Webcam'."""
    b = re.sub(r'-video-index\d+$', '', re.sub(r'^usb-', '', os.path.basename(path)))
    parts = [p for p in b.split('_') if p]
    if len(parts) > 1 and len(parts[-1]) >= 8 and re.search(r'\d', parts[-1]):
        parts = parts[:-1]                                   # serial number
    if len(parts) > 3 and parts[1].lower() in ('inc', 'inc.', 'ltd', 'co', 'corp', 'llc', 'gmbh'):
        parts = parts[2:]                                    # vendor "Nexight Inc"
    return (' '.join(parts) or 'USB camera')[:40]


def usb_mic(video_path, sysfs='/sys'):
    """ALSA name ('hw:CARD=Webcam') of the mic built into the same USB device as this webcam, or ''."""
    try:
        node = os.path.basename(os.path.realpath(video_path))
        vdev = os.path.dirname(os.path.realpath(os.path.join(sysfs, 'class/video4linux', node, 'device')))
        for card in sorted(glob.glob(os.path.join(sysfs, 'class/sound/card*'))):
            if os.path.dirname(os.path.realpath(os.path.join(card, 'device'))) == vdev:
                cid = open(os.path.join(card, 'id')).read().strip()
                if cid:
                    return f'hw:CARD={cid}'
    except OSError:
        pass
    return ''


def ipw_base(url):
    """'http://[user:pw@]host:8080/video' (IP Webcam / DroidCam style) -> 'http://[user:pw@]host:8080', else ''."""
    m = re.match(r'(?i)^(https?://[^/?#]+)/video/?$', str(url or ''))
    return m.group(1) if m else ''


def host_port(url):
    u = urllib.parse.urlsplit(url)
    try:
        port = u.port
    except ValueError:
        port = None
    return (u.hostname or '', port or {'https': 443, 'rtsp': 554}.get(u.scheme.lower(), 80))


def _http_json(url, timeout):
    """GET a small JSON document; user:password in the URL becomes basic auth. Raises on failure."""
    u = urllib.parse.urlsplit(url)
    netloc = u.hostname + (f':{u.port}' if u.port else '')
    req = urllib.request.Request(urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, '')))
    if u.username is not None:
        tok = base64.b64encode(f'{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or "")}'.encode()).decode()
        req.add_header('Authorization', 'Basic ' + tok)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(262144) or b'{}')


def probe_net(url, timeout=1.5):
    """Is this network camera reachable? -> (online, info). IP Webcam answers /status.json (info carries
    its orientation setting); anything else only has to accept a TCP connection. Never opens the video."""
    base = ipw_base(url)
    if base:
        try:
            d = _http_json(base + '/status.json', timeout)
            cur = d.get('curvals') if isinstance(d, dict) else None
            if isinstance(cur, dict):
                return True, {'ipw': True, 'orientation': str(cur.get('orientation') or 'landscape')}
            return True, {}
        except urllib.error.HTTPError:
            return True, {}                                   # it answered (DroidCam, auth, ...): it's there
        except Exception:
            return False, {}
    try:
        socket.create_connection(host_port(url), timeout).close()
        return True, {}
    except OSError:
        return False, {}


def read_accel(base, timeout=1.0):
    """IP Webcam accelerometer (needs sensors enabled in the app) -> (x, y, z) or None."""
    try:
        d = _http_json(base + '/sensors.json?sense=accel', timeout)
        data = (d.get('accel') or {}).get('data') or []
        v = data[-1][1]
        return float(v[0]), float(v[1]), float(v[2])
    except Exception:
        return None


def phys_rotation(x, y):
    """Which way up the phone is held, from gravity: '0' landscape (top to the left), '90' portrait,
    '180' landscape the other way, '270' upside-down portrait; None when flat or in between."""
    ax, ay = abs(x), abs(y)
    if max(ax, ay) < 4.0 or abs(ax - ay) < 2.0:
        return None
    if ax > ay:
        return '0' if x > 0 else '180'
    return '90' if y > 0 else '270'


def local_subnets():
    """[(prefix 'a.b.c.', own ip)] for the Pi's IPv4 networks (the /24 around each address)."""
    out = []
    try:
        txt = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        m = re.search(r'inet (\d+\.\d+\.\d+\.)(\d+)/(\d+)', line)
        if m and (m.group(1), m.group(1) + m.group(2)) not in out:
            out.append((m.group(1), m.group(1) + m.group(2)))
    return out[:3]


def find_device(configured=''):
    """The webcam's capture node: the configured path, else the first USB camera by stable id
    (/dev/video numbers shift around the Pi 5's other video nodes), else ''."""
    if configured:
        return configured if os.path.exists(configured) else ''
    found = sorted(glob.glob('/dev/v4l/by-id/*-video-index0'))
    return found[0] if found else ''


class Cam:
    def __init__(self, cfg, listener, feeds, probe, ctl_path, on_status=None, save=None,
                 out_url='rtsp://127.0.0.1:8554/cam', video_input=None, output=None, net_probe=None,
                 accel=None):
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
        raw = str(self.cfg.get('video_url') or '').strip()
        self._cfg_url = clean_url(raw)            # network camera from mixer_config.json (v3.6)
        if raw and not self._cfg_url:
            print('[mixer] cam: video_url ignored (must start with http://, https:// or rtsp://)', flush=True)
        self.cams = []                            # Wi-Fi cameras added on the page (v3.8)
        for n in self.cfg.get('cams') or []:
            try:
                u = clean_url(n.get('url'))
                if u and re.match(r'^n[0-9a-f]{6}$', str(n.get('id'))):
                    self.cams.append({'id': n['id'], 'name': str(n.get('name') or 'Wi-Fi camera')[:30], 'url': u})
            except AttributeError:
                pass
        self.cams = self.cams[:MAX_NET_CAMS]
        self.choice = str(self.cfg.get('source') or '')
        self.rot = {str(k): v for k, v in dict(self.cfg.get('rot') or {}).items() if v in ROT_MODES}
        self._net_probe = net_probe or probe_net
        self._accel = accel or read_accel
        self._online = {}                         # source id -> True / False (missing = not checked yet)
        self._info = {}                           # source id -> probe info (IP Webcam orientation ...)
        self._auto = {}                           # source id -> rotation the accelerometer asked for
        self._auto_cand = None
        self._auto_note = ''
        self._mic_bad = {}                        # source id -> time until which its mic is not tried again
        self._net_fails = {}                      # source id -> consecutive failed checks
        self._src_cache = None
        self._active = None                       # the source the running encoder uses
        self.audio_kind = ''                      # '' | 'console' | 'mic'
        self.epoch = 0                            # +1 per encoder start: pages reconnect when it changes
        self._delay_timer = None
        self._probing = False
        self._output = output                     # tests: replaces the RTSP output args
        q = self.cfg.get('quality', DEFAULT_QUALITY)
        self.quality = q if q in QUALITIES else DEFAULT_QUALITY
        self.feed = str(self.cfg.get('feed') or 'main1')
        ab = str(self.cfg.get('audio_bitrate') or DEFAULT_AUDIO_RATE)
        self.audio_bitrate = ab if ab in AUDIO_RATES else DEFAULT_AUDIO_RATE
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
        """Console pair for the chosen feed ('mic' falls back to Main LR when the camera has no mic)."""
        want = 'main1' if self.feed == 'mic' else self.feed
        for f in self.feeds() or []:
            if f['id'] == want:
                return f['usb'][0] - 1, f['usb'][1] - 1
        return None

    def _audio_plan(self, src):
        """'mic' (the camera's own mic), 'console' (a WING pair via the picker) or '' (picture only)."""
        now = time.time()
        if self.feed == 'mic' and src and src.get('mic') and now >= self._mic_bad.get(src.get('id'), 0):
            return 'mic'
        if self._pair() is not None and now >= self._no_audio_until:
            return 'console'
        return ''

    def set_feed(self, fid):
        fid = str(fid)
        if fid != 'mic' and not any(f['id'] == fid for f in self.feeds() or []):
            return False
        self.feed = fid
        self._no_audio_until = 0.0                                # chosen again: try that audio again
        self._mic_bad = {}
        self._write_ctl()
        self._saved(); self._publish()
        if self.running and self._audio_plan(self._active) != self.audio_kind:
            self._restart('sound source change')
        return True

    def set_delay(self, ms):
        self.delay_ms = self._clamp_delay(ms)
        self._write_ctl()                                         # console sound: the picker applies it live
        self._saved(); self._publish()
        if self.running and self.audio_kind == 'mic':             # camera mic: a short restart, once dragging stops
            if self._delay_timer:
                self._delay_timer.cancel()
            self._delay_timer = threading.Timer(0.8, lambda: self.running and self.audio_kind == 'mic'
                                                and self._restart('mic delay change'))
            self._delay_timer.daemon = True
            self._delay_timer.start()
        return self.delay_ms

    # ── cameras (v3.8) ──
    def sources(self):
        """Every camera the Pi knows: USB webcams found now + network cameras (config + added on the page)."""
        c = self._src_cache
        if c and time.time() - c[0] < 2.0:
            return c[1]
        out = []
        if self._video_input:
            out.append({'id': 'test', 'kind': 'test', 'name': 'Test camera', 'mic': ''})
        for p in usb_cameras(self.cfg.get('device', '')):
            out.append({'id': 'usb:' + os.path.basename(p), 'kind': 'usb', 'name': usb_name(p), 'dev': p,
                        'mic': usb_mic(p)})
        nets = ([{'id': 'net0', 'name': str(self.cfg.get('video_name') or 'Network camera')[:30],
                  'url': self._cfg_url, 'fixed': True}] if self._cfg_url else []) + self.cams
        for n in nets:
            b = ipw_base(n['url'])
            mic = b + '/audio.wav' if b and ':4747' not in b else ('same' if n['url'].lower().startswith('rtsp://') else '')
            out.append({**n, 'kind': 'net', 'mic': mic, 'ipw': bool(b) and ':4747' not in b})
        self._src_cache = (time.time(), out)
        return out

    def active_source(self):
        """The chosen camera while it is reachable; else the first USB webcam; else a reachable Wi-Fi one."""
        srcs = self.sources()
        for s in srcs:
            if s['id'] == self.choice and self._online.get(s['id']) is not False:
                return s
        for s in srcs:
            if s['kind'] in ('test', 'usb'):
                return s
        for s in srcs:
            if s['kind'] == 'net' and self._online.get(s['id']) is not False:
                return s
        return None

    @property
    def url(self):
        """URL of the network camera that would be used ('' for USB) -- never sent to the page."""
        s = self._active if self.running else self.active_source()
        return s['url'] if s and s['kind'] == 'net' else ''

    def _eff_rot(self, src):
        if not src:
            return '0'
        mode = self.rot.get(src['id'], '0')
        return self._auto.get(src['id'], '0') if mode == 'auto' else mode

    def _follow(self, why):
        """The camera that should be on air changed (choice, online state): move the encoder over."""
        if self.running:
            want = self.active_source()
            if want and (not self._active or want['id'] != self._active['id']):
                self._restart(why)
                return
        self._publish()

    def set_source(self, sid):
        if not any(s['id'] == sid for s in self.sources()):
            return False
        self.choice = sid
        self._fails = []
        self._saved()
        if sid in self._online and not self._online[sid]:
            self._online.pop(sid)                                 # chosen again: give it a fresh try
        self._follow('camera change')
        return True

    def set_rotate(self, mode, sid=''):
        mode = str(mode)
        src = next((s for s in self.sources() if s['id'] == sid), None) if sid else (self._active if self.running else self.active_source())
        if mode not in ROT_MODES or not src or (mode == 'auto' and not src.get('ipw')):
            return False
        before = self._eff_rot(src)
        self.rot[src['id']] = mode
        self._saved()
        if self.running and self._active and self._active['id'] == src['id'] and self._eff_rot(src) != before:
            self._restart('rotation change')
        self._publish()
        return True

    def add_camera(self, name, url):
        url = clean_url(url)
        if not url or len(self.cams) >= MAX_NET_CAMS:
            return None
        if any(host_port(c['url']) == host_port(url) and c['url'] == url for c in self.cams):
            return next(c['id'] for c in self.cams if c['url'] == url)
        cid = 'n' + secrets.token_hex(3)
        self.cams.append({'id': cid, 'name': (str(name or '').strip() or 'Wi-Fi camera')[:30], 'url': url})
        self._src_cache = None
        self._saved()
        threading.Thread(target=self.probe_once, daemon=True).start()
        self._publish()
        return cid

    def remove_camera(self, cid):
        if not any(c['id'] == cid for c in self.cams):
            return False
        self.cams = [c for c in self.cams if c['id'] != cid]
        self.rot.pop(cid, None); self._online.pop(cid, None); self._info.pop(cid, None)
        if self.choice == cid:
            self.choice = ''
        self._src_cache = None
        self._saved()
        self._follow('camera removed')
        return True

    def find_cameras(self, timeout=0.4):
        """Scan the Pi's networks for phones running IP Webcam (port 8080) or DroidCam (4747)."""
        hosts = [p + str(i) for p, own in local_subnets() for i in range(1, 255) if p + str(i) != own]

        def check(ip):
            for port in (8080, 4747):
                try:
                    socket.create_connection((ip, port), timeout).close()
                except OSError:
                    continue
                if port == 4747:
                    return {'name': f'DroidCam {ip}', 'url': f'http://{ip}:4747/video'}
                ok, info = self._net_probe(f'http://{ip}:8080/video', 1.5)
                if info.get('ipw'):
                    return {'name': f'IP Webcam {ip}', 'url': f'http://{ip}:8080/video'}
            return None
        with ThreadPoolExecutor(64) as ex:
            res = [r for r in ex.map(check, hosts) if r]
        known = {host_port(s['url']) for s in self.sources() if s['kind'] == 'net'}
        return [r for r in res if host_port(r['url']) not in known]

    def probe_once(self):
        """Check every network camera once; follow the choice when its camera comes and goes."""
        self._src_cache = None
        changed = False
        for s in self.sources():
            if s['kind'] != 'net':
                continue
            proc = self._proc
            if self.running and self._active and self._active['id'] == s['id'] and proc and proc.poll() is None \
                    and time.time() - self._started_at > 5:
                ok, info = True, {}                              # streaming from it right now: that's proof enough
            else:
                try:
                    ok, info = self._net_probe(s['url'], 2.5)
                except Exception:
                    ok, info = False, {}
            if not ok and self._online.get(s['id']):           # one missed answer on busy Wi-Fi is not "offline"
                self._net_fails[s['id']] = self._net_fails.get(s['id'], 0) + 1
                if self._net_fails[s['id']] < 2:
                    continue
            self._net_fails[s['id']] = 0
            if self._online.get(s['id']) != ok:
                changed = True
                print(f"[mixer] cam: {s['name']} {'online' if ok else 'offline'}", flush=True)
            self._online[s['id']] = ok
            if info:
                self._info[s['id']] = info
        if changed:
            self._follow('camera ' + ('back' if self.choice else 'online/offline'))

    def start_probe(self, every=5.0):
        if self._probing or not self.enabled:
            return
        self._probing = True

        def loop():
            last = 0.0
            while True:
                try:
                    if time.time() - last >= every:
                        last = time.time()
                        self.probe_once()
                    self._auto_step()
                except Exception as e:
                    print(f'[mixer] cam: probe error: {e}', flush=True)
                time.sleep(1.0)
        threading.Thread(target=loop, daemon=True, name='cam-probe').start()

    def _auto_step(self):
        """Auto rotation for IP Webcam: follow the phone's accelerometer (needs sensors on in the app)."""
        act = self._active if self.running else None
        if not act or not act.get('ipw') or self.rot.get(act['id']) != 'auto':
            if self._auto_note:
                self._auto_note = ''
                self._publish()
            return
        acc = self._accel(ipw_base(act['url']))
        if acc is None:
            note = 'auto-rotate needs the phone\'s sensor data: turn on sensors in IP Webcam\'s settings'
            if self._auto_note != note:
                self._auto_note = note
                self._publish()
            return
        if self._auto_note:
            self._auto_note = ''
            self._publish()
        phys = phys_rotation(acc[0], acc[1])
        if phys is None:
            return
        base = _IPW_BASE.get(self._info.get(act['id'], {}).get('orientation', 'landscape'), 0)
        eff = str((int(phys) - base) % 360)
        now = time.time()
        if eff == self._auto.get(act['id'], '0'):
            self._auto_cand = None
        elif not self._auto_cand or self._auto_cand[0] != eff:
            self._auto_cand = (eff, now)
        elif now - self._auto_cand[1] >= 1.5:                  # held that way for 1.5 s: turn the picture
            self._auto[act['id']] = eff
            self._auto_cand = None
            self._restart('phone rotated')

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

    def set_audio_bitrate(self, rate):
        rate = str(rate)
        if rate not in AUDIO_RATES:
            return False
        changed = rate != self.audio_bitrate
        self.audio_bitrate = rate
        self._saved()
        if changed and self.running and self.audio:
            self._restart('audio bitrate change')
        self._publish()
        return True

    def _saved(self):
        try:
            self.save({'feed': self.feed, 'delay_ms': self.delay_ms, 'quality': self.quality,
                       'audio_bitrate': self.audio_bitrate, 'source': self.choice, 'rot': dict(self.rot),
                       'cams': [dict(c) for c in self.cams]})
        except Exception as e:
            print(f'[mixer] cam: could not save settings: {e}', flush=True)

    # ── state ──
    def device(self):
        s = self.active_source()
        return s['dev'] if s and s['kind'] == 'usb' else ''

    def available(self):
        return self.enabled and self.active_source() is not None

    def keepalive(self):
        """Listener watchdog: capture must keep running while the video carries console audio."""
        return self.running and self.audio_kind == 'console'

    def status(self):
        act = self._active if self.running else self.active_source()
        srcs = self.sources()
        return {
            'enabled':   self.enabled,
            'available': self.enabled and act is not None,
            'source':    'network' if act and act['kind'] == 'net' else 'usb',
            'sources':   [{'id': s['id'], 'name': s['name'], 'kind': s['kind'], 'online': self._online.get(s['id']),
                           'mic': bool(s['mic']), 'auto': bool(s.get('ipw')), 'rotate': self.rot.get(s['id'], '0'),
                           'fixed': bool(s.get('fixed'))} for s in srcs],
            'choice':    self.choice if any(s['id'] == self.choice for s in srcs) else (act['id'] if act else ''),
            'active':    act['id'] if act else '',
            'fallback':  bool(self.choice and act and act['id'] != self.choice and any(s['id'] == self.choice for s in srcs)),
            'rotate':    self._eff_rot(act),
            'rotate_mode': self.rot.get(act['id'], '0') if act else '0',
            'mic_ok':    bool(act and act.get('mic')) and time.time() >= self._mic_bad.get(act['id'], 0),
            'audio_kind': self.audio_kind if self.running else self._audio_plan(act),
            'auto_note': self._auto_note,
            'epoch':     self.epoch,
            'running':   self.running,
            'viewers':   self.readers,
            'audio':     self.audio if self.running else bool(self._audio_plan(act)),
            'feed':      self.feed,
            'delay_ms':  self.delay_ms,
            'quality':   self.quality,
            'qualities': [{'id': k, 'label': v['label']} for k, v in QUALITIES.items()],
            'audio_bitrate': self.audio_bitrate,
            'audio_rates': list(AUDIO_RATES),
            'max_delay': MAX_DELAY_MS,
            'error':     self.error,
            'audio_issue': self._audio_issue if self.running and (not self.audio or (self.feed == 'mic' and self.audio_kind != 'mic')) else '',
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
        pair = None if off or not (self.running and self.audio_kind == 'console' and self._fifo) else self._pair()
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
    def _net_input(self, url=None):
        """ffmpeg input args for the network camera. Wall-clock stamps keep A/V alignment the same as the
        USB webcam's; nobuffer / low_delay keep the extra hop from adding queueing."""
        url = url or self.url
        if url.lower().startswith('rtsp://'):
            tr = str(self.cfg.get('rtsp_transport') or 'tcp')
            tr = tr if tr in ('tcp', 'udp') else 'tcp'
            a = ['-rtsp_transport', tr, '-timeout', str(NET_TIMEOUT_US)]
        else:                                          # short Wi-Fi dropouts are absorbed without losing the stream
            a = ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '2',
                 '-rw_timeout', str(NET_TIMEOUT_US)]
        return a + ['-fflags', 'nobuffer', '-flags', 'low_delay', '-thread_queue_size', '512',
                    '-use_wallclock_as_timestamps', '1', '-i', url]

    def _mic_input(self, mic):
        """Second ffmpeg input for the camera's own mic ([] when it rides in the video input)."""
        if mic == 'same':
            return []
        if mic.startswith('hw:'):                                  # USB webcam mic: its own ALSA card
            return ['-thread_queue_size', '1024', '-f', 'alsa', '-channels', '1', '-sample_rate', '48000',
                    '-use_wallclock_as_timestamps', '1', '-i', mic]
        return ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '2',
                '-rw_timeout', str(NET_TIMEOUT_US), '-fflags', 'nobuffer', '-thread_queue_size', '1024',
                '-use_wallclock_as_timestamps', '1', '-i', mic]      # IP Webcam /audio.wav

    def _cmd(self, audio, src=None):
        if audio is True:
            audio = 'console'
        src = src or (self._active if self.running and self._active else self.active_source()) or {'kind': 'usb', 'dev': ''}
        q = QUALITIES[self.quality]
        fps = q['fps']
        kbps = int(q['bitrate'].rstrip('k'))
        c = []
        nice = int(self.cfg.get('nice', 15))
        if nice and shutil.which('nice'):
            c += ['nice', '-n', str(nice)]
        c += ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error']
        net = src['kind'] == 'net'
        if src['kind'] == 'test':
            c += list(self._video_input)
        elif net:
            c += self._net_input(src['url'])
        else:
            c += ['-thread_queue_size', '512', '-f', 'v4l2', '-input_format', 'mjpeg',
                  '-video_size', '1280x720', '-framerate', str(q.get('cap', fps)),
                  '-use_wallclock_as_timestamps', '1', '-i', src.get('dev', '')]
        amap = '1:a:0'
        if audio == 'console':
            c += ['-thread_queue_size', '1024', '-f', 's24le', '-ar', '48000', '-ac', '2',
                  '-use_wallclock_as_timestamps', '1', '-i', self._fifo]
        elif audio == 'mic':
            extra = self._mic_input(src.get('mic', ''))
            c += extra
            amap = '1:a:0' if extra else '0:a:0?'
        c += ['-copyts', '-start_at_zero']
        rot = self._eff_rot(src) if src.get('id') else '0'
        portrait = rot in ('90', '270')
        w, h = (q['h'], q['w']) if portrait else (q['w'], q['h'])
        vf = f'fps={fps}'                                      # drop frames first: less to turn / scale
        vf += {'90': ',transpose=1', '270': ',transpose=2', '180': ',hflip,vflip'}.get(rot, '')
        if net:                                                # the phone picks its own size: fit it (even dims)
            vf += (f',scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bilinear'
                   f',pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1')
        elif (q['w'], q['h']) != (1280, 720):
            vf += f",scale={w}:{h}:flags=bilinear"
        c += ['-map', '0:v:0', '-vf', vf,
              '-c:v', 'libx264', '-preset', q.get('preset', 'ultrafast'), '-tune', 'zerolatency',
              '-profile:v', 'baseline', '-pix_fmt', 'yuv420p',
              '-b:v', q['bitrate'], '-maxrate', q['bitrate'], '-bufsize', f'{2 * kbps}k',
              '-g', str(int(fps * float(self.cfg.get('gop_s', 2)))), '-x264-params', 'scenecut=0',
              '-threads', str(int(self.cfg.get('threads', 2)))]
        if audio:
            # async=1: line the sound up with its timestamps ONCE (pad/trim), never stretch it. The fifo's
            # wall-clock stamps jitter by tens of ms; stretching to follow them (async=1000, v3.4-3.6)
            # wobbled the pitch +-2% -- the "warbly cassette" sound. Clock drift beyond 0.3 s still gets
            # one small hard correction.
            # Camera mic: its delay is a filter (the encoder restarts when it changes), mono -> both sides.
            af = 'aresample=async=1:min_hard_comp=0.3'
            if audio == 'mic' and self.delay_ms:
                af = f'adelay={self.delay_ms}:all=1,' + af
            c += ['-map', amap, '-af', af,
                  '-c:a', 'libopus', '-b:a', self.audio_bitrate,
                  '-application', 'audio', '-ar', '48000', '-ac', '2']
        else:
            c += ['-an']
        c += ['-flush_packets', '1']
        c += list(self._output) if self._output else ['-f', 'rtsp', '-rtsp_transport', 'tcp', self.out_url]
        return c

    def _start(self):
        with self.lock:
            if self.running:
                return
            src = self.active_source() if self.enabled else None
            if not src:
                self.error = 'no camera found'
                self._publish()
                return
            self._gen += 1
            gen = self._gen
            audio = self._audio_plan(src)                       # '' | 'console' | 'mic'
            if audio and not (self.feed == 'mic' and audio != 'mic' and src.get('mic')):
                self._audio_issue = ''                            # (a failed camera mic keeps saying why)
            self._active = src
            self.audio_kind = audio
            self.audio, self.error, self.ready, self.readers = bool(audio), '', False, 0
            self._tail = []
            self._started_at = time.time()
            try:
                if audio == 'console':
                    self._fifo = f'{self._fifo_base}_{gen}.pcm'      # fresh fifo per run: no stale audio
                    try:
                        os.remove(self._fifo)
                    except OSError:
                        pass
                    os.mkfifo(self._fifo, 0o600)
                    self._holder = os.open(self._fifo, os.O_RDWR | os.O_NONBLOCK)   # keeps a reader present
                self._proc = subprocess.Popen(self._cmd(audio, src), stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as e:
                self.error = f'start failed: {e}'
                self._cleanup()
                self._publish()
                return
            self.running = True
            self.epoch += 1
            proc = self._proc
        if audio == 'console':
            self.listener.ensure_capture()                     # the audio pipeline must be up (outside our lock)
        threading.Thread(target=self._drain, args=(gen, proc), daemon=True, name='cam-err').start()
        threading.Thread(target=self._supervise, args=(gen, proc, audio), daemon=True, name='cam-sup').start()
        self._publish()

    def _drain(self, gen, proc):
        for raw in iter(proc.stderr.readline, b''):
            t = redact(raw.decode(errors='replace').strip())
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

    def _wait_fifo_open(self, proc, gen, timeout=12.0):
        fifo = os.path.realpath(self._fifo) if self._fifo else ''
        fd_dir = f'/proc/{proc.pid}/fd'
        end = time.time() + timeout
        while time.time() < end and gen == self._gen and proc.poll() is None:
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        if os.readlink(os.path.join(fd_dir, fd)) == fifo:
                            time.sleep(0.05)
                            return True
                    except OSError:
                        pass
            except OSError:                                    # no /proc (not Linux): the old fixed wait
                time.sleep(0.4)
                return False
            time.sleep(0.05)
        return False

    def _supervise(self, gen, proc, audio):
        # Audio starts flowing only once ffmpeg has the fifo open: until then the picker's writes pile up
        # in the pipe (up to ~0.22 s) and would be stamped late when finally read -- a constant A/V
        # offset after every start (worst with a Wi-Fi camera, whose input opens slowly). So wait until
        # ffmpeg really holds the fifo (/proc/<pid>/fd), then switch the picker's cam output on.
        if audio == 'console':
            self._wait_fifo_open(proc, gen)
        else:
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
            if audio == 'console':
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
                act = self._active or {}
                if audio == 'mic' and now - self._started_at < 10:      # the camera mic won't open: go on without it
                    self._mic_bad[act.get('id')] = now + 60
                    self._audio_issue = ('camera mic unavailable: ' + why)[:120]
                    self._restart('camera mic failed: ' + self.error)
                    return
                if act.get('kind') == 'net':                         # Wi-Fi camera gone? then fall back to USB
                    try:
                        ok, _ = self._net_probe(act['url'])
                    except Exception:
                        ok = False
                    self._online[act['id']] = ok
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
