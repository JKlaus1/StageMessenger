"""
WING / X32 / M32 remote mixer + listen-back for Stage Messenger.

The console is found, not configured (v3.3): discover.py asks the mixer interface (eth0) for any
WING ('WING?' on 2222) or X32 / M32 ('/xinfo' on 10023) and the driver follows whatever answers. A
watcher keeps looking whenever the console is gone and hot-swaps the driver (WING <-> X32, or a new
DHCP address) without restarting the service; open pages reload into the new layout. mixer_config.json
"mixer_type" can still force 'wing' / 'x32' and "mixer_ip" can pin an address, but neither is needed.
Both drivers speak the WING address dialect to this module and the page; x32.py translates. Features
the X32 path doesn't have yet are switched off through CAPS (the page sizes itself from them).

Everything lives under /mixer so one Cloudflare Access path rule protects the page,
the API and the audio stream. Mixer traffic deliberately does NOT use Socket.IO: the
/socket.io endpoint is shared with the public singer pages and can't be gated by path.

Remote (tunnel) requests are refused unless mixer_config.json sets
"remote_enabled": true -- flip it only after the Access rule for /mixer exists.
"""
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, Response, jsonify, request, send_from_directory, abort

from .wing import Wing, N_BUS, N_MTX, N_CH, N_AUX, N_SD, N_MAIN, N_DCA, SRC_GROUPS, SRC_LEAVES, parse_describe, osc_msg, osc_parse
from .listen import Listener
from .cam import Cam
from .meters import Meters
from . import x32 as x32mod
from .discover import Finder

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(HERE), 'mixer_config.json')
STATE_PATH = os.path.join(os.path.dirname(HERE), 'mixer_state.json')

DEFAULTS = {
    'mixer_ip':        '',       # '' = find the console (v3.3); an address here pins it
    'mixer_type':      'auto',   # 'wing' | 'x32' (X32 / M32 family) | 'auto' = whichever answers
    'mixer_iface':     'eth0',   # discovery searches only this interface (the rack / mixer network)
    'mixer_scan':      [],       # extra addresses to ask (unicast) -- normally empty
    'watch_s':         3,        # console watcher: how often to look while no console is connected
    'remote_enabled':  False,
    'usb_patch':       True,     # the Pi owns WING USB outs 1-40, 42-48 (v3.9 layout, see feed_table)
                                 # and, on an X32, User Out 3-8 + the Card out 25-32 block (blocks 1-24 untouched)
    'ambient': {                 # WING USB 42 / X32 User Out 3: room/stage ambient mic
        'follow_channel': 10,    # default channel whose input source is used (the page can pick another, v3.9)
        'grp': 'B', 'in': 4,     # WING fallback when that channel reports no source
    },
    # listen-back capture per console (v3.9): ALSA device + channel count (both S24_3LE 48 kHz)
    'capture': {
        'wing': {'device': 'hw:WING', 'channels': 48},
        'x32':  {'device': 'hw:XLIVE', 'channels': 32},
    },
    'bitrate':         '128k',
    # listen-back latency (v1.10)
    'cushion_s':       0.5,      # audio handed to a new listener up front (was 1.5)
    'max_queue_s':     1.0,      # a listener further behind than this on the Pi is skipped to live
    'listen_target_s': 0.8,      # how much audio the page keeps buffered; it trims anything beyond
    # low-latency listen-back via WebRTC (v2.0): MediaMTX on the Pi + Cloudflare TURN for remote ears
    'rtc': {
        'enabled':        True,
        'rtsp':           'rtsp://127.0.0.1:8554/listen',
        'api':            'http://127.0.0.1:9997',
        'whep':           'http://127.0.0.1:8889/listen/whep',
        'opus_bitrate':   '128k',    # starting value; the Listen card's choice is saved and wins (v3.5)
        'turn_key_id':    '',        # Cloudflare Realtime TURN key -- set these in mixer_config.json,
        'turn_api_token': '',        # never in git
        'turn_ttl':       86400,
    },
    # video feed (v3.4): a USB webcam + a console feed as one WebRTC stream on MediaMTX's "cam" path.
    # Separate from listen-back; runs only while someone watches. Needs rtc.enabled (MediaMTX).
    'cam': {
        'enabled':       True,
        'device':        '',         # '' = first USB camera under /dev/v4l/by-id
        'video_url':     '',         # network camera instead of USB (v3.6): http(s):// MJPEG or rtsp:// -- set in
                                     # mixer_config.json (it may hold a password), e.g. an Android phone running
                                     # IP Webcam: "http://192.168.1.50:8080/video"
        'rtsp_transport': 'tcp',     # for rtsp:// sources: tcp (robust on Wi-Fi) | udp (a little lower latency)
        'rtsp':          'rtsp://127.0.0.1:8554/cam',
        'whep':          'http://127.0.0.1:8889/cam/whep',
        'audio_bitrate': '128k',     # Opus for the video's sound (64k | 96k | 128k | 160k)
        'idle_s':        15,         # stop the encoder this long after the last viewer leaves
        'nice':          15,         # ffmpeg priority (listen-back and the DMX engine keep theirs)
        'threads':       2,          # x264 threads
        'gop_s':         2,          # keyframe interval, seconds (YouTube wants <= 4)
        # starting values; changes made on the page are saved in mixer_state.json and win
        'quality':       'good',     # high | good | medium | low | min (see mixer/cam.py)
        'feed':          'main1',    # console feed whose audio goes with the picture
        'delay_ms':      0,          # audio delay to line sound up with the (later) video
    },
    # Spotify card (v2.4): go-librespot "Stage Rig" -> WING USB 1/2 -> AUX 1 (see mixer/spotify.py)
    'spotify': {
        'enabled':    True,
        'api':        'http://127.0.0.1:3678',
        'aux':        1,                              # the WING aux strip USB 1/2 feeds
        'config_dir': '/home/pi/.config/go-librespot',
        # search (v2.5): your own Spotify developer app, client-credentials (app-only, never your
        # account). Set with mixer/set_spotify_search.sh -- secrets live only in mixer_config.json.
        'search_client_id':     '',
        'search_client_secret': '',
        'search_saved_at':      '',        # ISO date the secret was saved (stamped on first start if missing)
        'search_secret_days':   180,       # Spotify dashboard: client secret lifetime -> renewal warning
        'market':               'US',
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[mixer] bad {CONFIG_PATH}: {e} (using defaults)', flush=True)
    return cfg


# ── USB patch / listen feeds ────────────────────────────────────────────────────
# WING (v3.9 layout -- the last 8 USB outs are the "listen block", same idea as the X32's card 25-32):
#   1-32 Bus 1-16 L/R | 33-40 Mtx 1-4 L/R | 41 free | 42 ambient | 43/44 Main 1 (LR) | 45/46 Main 2 (subs)
#   | 47/48 Monitor 1 (phones / solo)
# Assumption (verify by ear): stereo sources occupy consecutive 'in' indices, e.g. BUS in 1/2 = Bus 1 L/R,
# MAIN in 3/4 = Main 2 L/R.
# X32 / M32 (X-LIVE USB, 32 ch): the Card out 25-32 block carries User Out 1-8 (block value 26 = UOUT1-8);
# the Pi owns User Out 3-8. Source codes verified with the oscillator on an X32 Rack fw 4.15 (2026-10-06):
#   169-184 = Local Out 1-16 (182 = Out 14), 207/208 = Monitor L/R; inputs 1-32 local, 33-80 AES50 A,
#   81-128 AES50 B, 129-160 card, 161-166 aux in.

def feed_table(console='wing'):
    """[(feed_id, usb_left_1based, usb_right_1based, grp, in_left, in_right)] -- WING patch sources;
    X32 rows carry the User Out source codes instead (grp 'UOUT')."""
    if console == 'x32':
        return [('main1', 31, 32, 'UOUT', X32_UOUT['main_l'], X32_UOUT['main_r']),
                ('main2', 28, 28, 'UOUT', X32_UOUT['sub'], X32_UOUT['sub']),
                ('mon1', 29, 30, 'UOUT', X32_UOUT['mon_l'], X32_UOUT['mon_r'])]
    t = [('main1', 43, 44, 'MAIN', 1, 2), ('main2', 45, 46, 'MAIN', 3, 4), ('mon1', 47, 48, 'MON', 1, 2)]
    for b in range(1, N_BUS + 1):
        u = 1 + 2 * (b - 1)
        t.append((f'bus{b}', u, u + 1, 'BUS', 2 * b - 1, 2 * b))
    for m in range(1, 5):
        u = 33 + 2 * (m - 1)
        t.append((f'mtx{m}', u, u + 1, 'MTX', 2 * m - 1, 2 * m))
    return t

AMBIENT_USB = 42                    # WING USB out (v3.9; was 43)
X32_AMBIENT_USB = 27                # X-LIVE USB 27 = Card out 27 = User Out 3
X32_CARD_BLOCK = '25-32'
X32_CARD_UOUT18 = 26                # /config/routing/CARD/25-32 value for "UOUT1-8"
X32_UOUT = {'main_l': 183, 'main_r': 184, 'sub': 182, 'mon_l': 207, 'mon_r': 208}   # Out 15/16, Out 14, Mon L/R
X32_UOUT_SLOTS = {3: 'ambient', 4: 'sub', 5: 'mon_l', 6: 'mon_r', 7: 'main_l', 8: 'main_r'}
X32_SRC_BASE = {'LCL': 0, 'A': 32, 'B': 80, 'CRD': 128, 'AUX': 160}
X32_SRC_MAX = {'LCL': 32, 'A': 48, 'B': 48, 'CRD': 32, 'AUX': 6}
SUB_DB_RANGE = (-30.0, 12.0)


def x32_input_code(grp, idx):
    """Physical input (driver group code + 1-based index) -> User Out source code, or 0."""
    if grp in X32_SRC_BASE and isinstance(idx, int) and 1 <= idx <= X32_SRC_MAX[grp]:
        return X32_SRC_BASE[grp] + idx
    return 0

# WING input-source group codes -> labels shown in the UI
SRC_NAMES = {'LCL': 'Local', 'A': 'AES A', 'B': 'AES B', 'C': 'AES C', 'SC': 'StageCon',
             'USB': 'USB', 'CRD': 'Card', 'MOD': 'Module', 'PLAY': 'Player', 'AES': 'AES/EBU',
             'USR': 'User', 'OSC': 'Osc', 'AUX': 'Aux'}


# ── console capabilities (the page sizes itself from these) ──────────────────────
CAPS = {
    'wing': {'console': 'wing', 'model': 'WING', 'nch': N_CH, 'naux': N_AUX, 'nbus': N_BUS, 'nmtx': N_MTX,
             'nmg': 8, 'main2': 'Main 2', 'sheet': True, 'recorder': 'wlive', 'listen': True, 'spotify': True,
             'alt': True, 'gain': [-2.5, 45], 'lcf': [20, 2000], 'hc': True, 'spol': True,
             'auxproc': ['eq', 'gate', 'dyn'], 'grdb': False, 'sd': N_SD, 'movemark': True, 'recauto': True,
             # v4.0 console view: mains / DCAs, who can send to a matrix, sends-on-fader to Main 2-4
             'nmain': N_MAIN, 'ndca': N_DCA, 'mtxsrc': ['ch', 'aux', 'bus', 'main'], 'mainsof': True, 'nfxr': 0},
    'x32':  {'console': 'x32', 'model': 'X32', 'nch': x32mod.N_CH, 'naux': x32mod.N_AUX, 'nbus': x32mod.N_BUS,
             'nmtx': x32mod.N_MTX, 'nmg': x32mod.N_MGRP, 'main2': 'M/C', 'sheet': True, 'recorder': 'xlive',
             'listen': True, 'spotify': False, 'alt': False, 'gain': list(x32mod.GAIN_RANGE),
             'lcf': list(x32mod.LCF_RANGE), 'hc': False, 'spol': False, 'auxproc': ['eq'], 'grdb': True,
             'sd': 1, 'movemark': False, 'recauto': False,
             'nmain': x32mod.N_MAIN, 'ndca': x32mod.N_DCA, 'mtxsrc': ['bus', 'main'], 'mainsof': False,
             'nfxr': x32mod.N_FXR},
}


def forced_type(cfg):
    t = str(cfg.get('mixer_type', 'auto')).lower()
    return {'m32': 'x32', 'x32': 'x32', 'wing': 'wing'}.get(t)


# ── SSE hub ─────────────────────────────────────────────────────────────────────

class Hub:
    def __init__(self):
        self.subs = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, msg):
        data = json.dumps(msg, separators=(',', ':'))
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


# ── Controller ──────────────────────────────────────────────────────────────────

SETTABLE = re.compile(
    r'^/(?:(?:ch|aux)/\d{1,2}/(?:fdr|mute|\$solo|send/\d{1,2}/(?:lvl|on))'
    r'|(?:bus|main|mtx)/\d{1,2}/(?:fdr|mute)'
    # v4.0 console view: solo on every strip kind, DCAs, pan, sends to the mains and the matrices
    r'|(?:bus|main|mtx|dca)/\d{1,2}/\$solo|dca/\d{1,2}/(?:fdr|mute)'
    r'|(?:ch|aux|bus|main|mtx|fxr)/\d{1,2}/pan'
    r'|(?:ch|aux|bus|fxr)/\d{1,2}/main/[1-4]/(?:lvl|on)'
    r'|fxr/\d{1,2}/(?:fdr|mute|\$solo|send/\d{1,2}/(?:lvl|on))'
    r'|(?:ch|aux|bus|main)/\d{1,2}/send/MX[1-8]/(?:lvl|on)'
    r'|ch/\d{1,2}/flt/(?:lcs|hcs)'
    r'|(?:ch|aux)/\d{1,2}/send/\d{1,2}/mode'
    r'|mgrp/[1-8]/mute'
    r'|(?:ch|aux)/\d{1,2}/(?:in/set/(?:trim|inv)|flt/(?:lc|lcf|hc|hcf))'
    r'|io/in/(?:LCL|A|B|C|SC|USB|CRD|MOD|PLAY|AES)/\d{1,2}/(?:g|vph|pol)'
    r'|io/altsw|cards/wlive/auto_(?:play|rec|stop))$')
LOGGED_SETS = ('/io/altsw', '/cards/wlive/auto_')   # console-wide changes: note who made them
SRC_COUNT = dict(SRC_GROUPS)
NODE_PATH = re.compile(r'^/(ch|aux|bus|main|mtx|fxr)/(\d{1,2})/(eq|gate|dyn)$')   # v4.0.3: output strips (eq / dyn)
LAYOUT_ITEM = re.compile(r'^(ch|aux|bus|main|mtx|dca|fxr)/(\d{1,2})$')
N_LAYERS, MAX_PROFILES = 3, 24
NODE_LOCKED = ('mdl',)          # model changes stay at the console for now


def _node_ok(path):
    if _mixer is not None and _mixer.x32:
        return _mixer.wing._node_target(path) is not None
    m = NODE_PATH.match(path or '')
    if not m or (m.group(3) == 'gate' and m.group(1) not in ('ch', 'aux')):
        return False
    return 1 <= int(m.group(2)) <= {'ch': N_CH, 'aux': N_AUX, 'bus': N_BUS, 'main': N_MAIN, 'mtx': N_MTX, 'fxr': 0}[m.group(1)]


class Mixer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hub = Hub()
        self.feed_id = 'main1'
        self.ambient_label = 'Ambient mic'
        self.node_cache = {}
        self._ovr_lock = threading.RLock()
        self._state_raw = self._read_state_file()
        self.patch_note = ''
        self._last_patch = 0.0
        self._patch_lock = threading.Lock()
        self._swap_lock = threading.RLock()
        self._started = False
        self._gen = 0                                 # driver generation: callbacks from a swapped-out driver are dropped
        self.finder = Finder(cfg.get('mixer_iface', 'eth0'), cfg.get('mixer_scan') or [])
        self.pinned = str(cfg.get('mixer_ip') or '').strip()
        self.want = forced_type(cfg)
        self._rec_pub = {}                                        # throttle for 10 Hz etime / sdfree pushes
        self._rec_tail = set()                                    # throttled addrs awaiting a trailing publish
        found = self._find(sweep=False)          # quick at boot; the watcher sweeps the subnet
        if found:
            kind, ip, info, how = found['kind'], found['ip'], found, 'found'
        else:
            # nothing answered: no driver traffic until the watcher finds one (then it swaps in)
            kind = self.want or (self._state_raw.get('console') if self._state_raw.get('console') in CAPS else 'wing')
            ip = self.pinned
            if self.want and self.pinned:
                info, how = None, 'configured'
            else:
                where = self.pinned or self.finder.last_where
                info, how = None, f'nothing answered on {where} -- still looking (last: {kind})'
        self._attach(kind, ip, info, how)
        rtc = cfg.get('rtc', {})
        self.rtc_enabled = bool(rtc.get('enabled'))
        self._turn, self._turn_exp, self._turn_lock = None, 0.0, threading.Lock()
        shm = '/dev/shm' if os.path.isdir('/dev/shm') else tempfile.gettempdir()
        self._cam_ctl = os.path.join(shm, f'wing_cam_{os.getpid()}')
        cam_cfg = dict(cfg.get('cam') or {})
        cam_cfg.update({k: v for k, v in (self._state_raw.get('cam') or {}).items()
                        if k in ('quality', 'feed', 'delay_ms', 'audio_bitrate', 'source', 'rot', 'cams')})
        cam_cfg['enabled'] = bool(cam_cfg.get('enabled', True)) and self.rtc_enabled
        lst = self._state_raw.get('listen') or {}
        self.sub_on = bool(lst.get('sub_on', False))            # v3.9 sub blend (shared, like the feed)
        try:
            self.sub_db = max(SUB_DB_RANGE[0], min(SUB_DB_RANGE[1], float(lst.get('sub_db', 0.0))))
        except (TypeError, ValueError):
            self.sub_db = 0.0
        cap_cmd, cap_ch = self._capture()
        self.listener = Listener(capture_cmd=cap_cmd, channels=cap_ch, bitrate=cfg.get('bitrate', '128k'),
                                 rtc_url=rtc.get('rtsp', '') if self.rtc_enabled else '',
                                 opus_bitrate=(self._state_raw.get('listen') or {}).get('opus_bitrate')
                                 or rtc.get('opus_bitrate', '128k'), rtc_probe=self.rtc_probe,
                                 cushion_s=cfg.get('cushion_s', 0.5), max_queue_s=cfg.get('max_queue_s', 1.0),
                                 on_status=lambda st: self.hub.publish({'t': 'listen', 's': st}),
                                 cam_ctl=self._cam_ctl if cam_cfg['enabled'] else '')
        self.select_feed(self.feed_id, publish=False)               # writes the route (blend) for the start
        self.cam = Cam(cam_cfg, self.listener, self.feeds, self.cam_probe, self._cam_ctl,
                       on_status=lambda st: self.hub.publish({'t': 'cam', 's': st}),
                       save=self._save_cam, out_url=cam_cfg.get('rtsp', 'rtsp://127.0.0.1:8554/cam'))
        if self.cam.enabled and cam_cfg.get('probe', True):
            self.cam.start_probe()                    # Wi-Fi cameras online / offline, auto-rotate (v3.8)
        self.spotify = None                           # the Spotify card is optional: never blocks the mixer
        self._ensure_spotify()

    # ── listen capture per console (v3.9) ──
    def _capture(self):
        from .listen import capture_cmd_for
        c = (self.cfg.get('capture') or {}).get(self.console) or DEFAULTS['capture'].get(self.console) \
            or DEFAULTS['capture']['wing']
        ch = int(c.get('channels', 48))
        return capture_cmd_for(c.get('device', 'hw:WING'), ch), ch

    def ambient_channel(self):
        """Channel whose input source feeds the ambient listen slot (page choice per console, else config)."""
        ch = (self._state_raw.get('ambient') or {}).get(self.console)
        if ch is None:
            ch = (self.cfg.get('ambient') or {}).get('follow_channel')
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            return None
        return ch if 1 <= ch <= self.caps['nch'] else None

    def set_ambient_channel(self, ch):
        ch = int(ch)
        if not 1 <= ch <= self.caps['nch']:
            return False
        with self._ovr_lock:
            amb = dict(self._state_raw.get('ambient') or {})
            amb[self.console] = ch
            self._state_raw = {**self._state_raw, 'ambient': amb}
            self._write_state()
        threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
        return True

    def set_sub_blend(self, on=None, db=None):
        if on is not None:
            self.sub_on = bool(on)
        if db is not None:
            self.sub_db = max(SUB_DB_RANGE[0], min(SUB_DB_RANGE[1], round(float(db), 1)))
        with self._ovr_lock:
            lst = dict(self._state_raw.get('listen') or {})
            lst.update(sub_on=self.sub_on, sub_db=self.sub_db)
            self._state_raw = {**self._state_raw, 'listen': lst}
            self._write_state()
        self.select_feed(self.feed_id, publish=False)

    # ── console: find / attach / watch / swap (v3.3) ──
    def _find(self, sweep=True):
        """-> discovery result dict or None. A pinned mixer_ip is asked alone (no sweep)."""
        try:
            if self.pinned:
                f = Finder('', [self.pinned], timeout=0.6).discover(sweep=False, want=self.want)
            else:
                f = self.finder.discover(hints=[self._state_raw.get('console_ip') or ''], sweep=sweep, want=self.want)
        except Exception as e:
            print(f'[mixer] discovery error: {e}', flush=True)
            return None
        if len(f) > 1:                                # several consoles answered: keep the one used last
            last = (self._state_raw.get('console_ip'), self._state_raw.get('console'))
            f.sort(key=lambda x: (x['ip'] != last[0], x['kind'] != last[1]))
            print(f"[mixer] {len(f)} consoles answered ({', '.join(x['model'] + ' ' + x['ip'] for x in f)}) "
                  f"-- using {f[0]['model']} (last used)", flush=True)
        return f[0] if f else None

    def _attach(self, kind, ip, info, how):
        """Build the console-specific half of the controller (caps, driver, meters, recorder lists,
        this console's channel order). Not started here."""
        self.console = kind
        self.x32 = kind == 'x32'
        self.ip = ip or ''
        self.found = bool(info) or how == 'configured'
        caps = dict(CAPS[kind])
        if info:
            caps['model'] = info.get('model') or caps['model']
            caps['fw'] = info.get('fw', '')
            caps['name'] = info.get('name', '')
        self.caps = caps
        where = self.ip or 'no address yet'
        print(f"[mixer] console: {caps['model']} at {where} ({how})", flush=True)
        self._load_state()
        self._gen += 1
        gen = self._gen
        cur = lambda: gen == self._gen
        # 'self.wing' is the console driver (wing.Wing or x32.X32 -- same interface)
        Driver = x32mod.X32 if self.x32 else Wing
        self.wing = Driver(self.ip,
                           on_update=lambda a, v: cur() and self._on_update(a, v),
                           on_conn=lambda ok: cur() and self._on_conn(ok),
                           on_loaded=lambda: cur() and self._on_loaded())
        if self.x32:
            from .x32meters import X32Meters
            self.meters = X32Meters(self.wing, wanted=lambda: bool(self.hub.subs))
        else:
            self.meters = Meters(self.ip, wanted=lambda: bool(self.hub.subs))
        self.n_sd = caps['sd']
        self.rec_markers = {n: [] for n in range(1, self.n_sd + 1)}   # card -> marker times (current session)
        self.rec_sessions = {n: [] for n in range(1, self.n_sd + 1)}  # card -> session list (WING newest first, X32 console order)
        self._rec_pub, self._rec_tail = {}, set()
        if self.found and (self._state_raw.get('console') != kind or self._state_raw.get('console_ip') != self.ip):
            self._write_state()

    def _ensure_spotify(self):
        if self.spotify or not self.caps['spotify'] or not (self.cfg.get('spotify') or {}).get('enabled'):
            return
        try:
            from .spotify import Spotify
            self.spotify = Spotify(self.cfg['spotify'], self.hub.publish, lambda: bool(self.hub.subs),
                                   cfg_path=CONFIG_PATH)
            if self._started:
                self.spotify.start()
        except Exception as e:
            print(f'[mixer] Spotify disabled: {e}', flush=True)
            self.spotify = None

    def _watch(self):
        """While no console is connected, look for one every watch_s; a different console (type or
        address) replaces the driver. A connected console is never second-guessed."""
        every = float(self.cfg.get('watch_s', 3) or 3)
        quiet_since, n, last_log = time.time(), 0, 0.0
        while True:
            time.sleep(every)
            d = self.wing
            if d.connected and time.time() - d.last_rx <= 2 * d.KA + 1:
                quiet_since = time.time()
                continue                              # liveness replies arrive every KA s
            if self.pinned and self.want:
                continue                              # fully configured: the driver reconnects by itself
            if time.time() - quiet_since < every:
                continue                              # give a fresh driver a moment to hear its console
            n += 1
            f = self._find(sweep=(n % 4 == 1))        # broadcast every pass, subnet sweep every 4th
            if not f:
                if time.time() - last_log > 60:
                    last_log = time.time()
                    print(f'[mixer] no console on {self.finder.last_where} -- still looking', flush=True)
                continue
            if f['kind'] == self.console and f['ip'] == self.ip:
                continue                              # same console: the driver will pick it back up
            self.swap(f)
            quiet_since, n = time.time(), 0

    def swap(self, info):
        """Hot-swap to the console in `info` (discovery result). Pages reload when the caps change."""
        with self._swap_lock:
            old_drv, old_m, old = self.wing, self.meters, f"{self.caps['model']} {self.ip or '-'}"
            self._gen += 1                            # silence the old driver's callbacks right away
            old_drv.stop(); old_m.stop()
            self._attach(info['kind'], info['ip'], info, f'switched from {old}')
            self._ensure_spotify()
            self.wing.start(); self.meters.start()
            self.patch_note = ''
            self.listener.set_capture(*self._capture())
            if self.caps['listen']:
                self.select_feed(self.feed_id if any(f['id'] == self.feed_id for f in self.feeds()) else 'main1')
        self.cam.console_changed()                    # its audio feed may have appeared or gone with the console
        self.hub.publish({'t': 'conn', 'ok': False})
        self.hub.publish({'t': 'snap', **self.snapshot()})

    def start(self):
        self._started = True
        self.wing.start()
        self.meters.start()
        threading.Thread(target=self._meter_pump, daemon=True, name='meter-pump').start()
        threading.Thread(target=self._routing_poll, daemon=True, name='routing-poll').start()
        threading.Thread(target=self._watch, daemon=True, name='console-watch').start()
        if self.spotify:
            try:
                self.spotify.start()
            except Exception as e:
                print(f'[mixer] Spotify disabled: {e}', flush=True)
                self.spotify = None

    def _strips(self):
        return [('ch', i) for i in range(1, self.caps['nch'] + 1)] + [('aux', i) for i in range(1, self.caps['naux'] + 1)]

    def _routing_poll(self):
        """The WING pushes nothing when a channel is re-patched, so re-read every strip's input
        patch and the physical-input settings (gain/48V/name) behind it every few seconds.
        (The X32 pushes re-patches and mutes -- nothing to poll.)"""
        n = 0
        while True:
            time.sleep(3)
            n += 1
            if self.x32:
                if self.wing.loaded and n % 5 == 0:             # X32 listen block: re-check every ~15 s
                    try:
                        self.ensure_patch()
                    except Exception as e:
                        print(f'[mixer] x32 listen patch: {e}', flush=True)
                continue
            if not self.wing.loaded:
                continue
            self.wing.poke([f'/{k}/{n}/in/conn/{leaf}' for k, n in self._strips()
                            for leaf in ('grp', 'in', 'altgrp', 'altin')]
                           + self._mute_addrs() + [f'/mgrp/{g}/mute' for g in range(1, 9)]
                           + [b + '/tags' for b in self.overrides])
            time.sleep(0.2)
            try:
                self.check_overrides()
            except Exception as e:
                print(f'[mixer] override check: {e}', flush=True)
            time.sleep(0.2)
            self.wing.poke(self._source_addrs())

    def _mute_addrs(self):
        return [f'/{k}/{n}/$mute' for k, n in self._strips()]

    def refresh_mutes(self, delay=0.15):
        if self.x32:
            return                                    # the X32 pushes every member's mix/on
        def go():
            time.sleep(delay)
            self.wing.poke(self._mute_addrs())
        threading.Thread(target=go, daemon=True).start()

    # ── mute-group override ──
    # The WING ignores OSC writes to $mute, so "unmute this channel while its group stays engaged"
    # is emulated by removing the engaged groups' '#Mn' tags from the channel and putting them back
    # when MUTE is pressed again or the group is released (from anywhere). Pending removals are kept
    # in mixer_state.json so a Pi restart can't strand a channel outside its group.
    @staticmethod
    def _tag_list(tags):
        return [t.strip() for t in str(tags or '').split(',') if t.strip()]

    @staticmethod
    def _read_state_file():
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f'[mixer] bad {STATE_PATH}: {e}', flush=True)
            return {}

    @property
    def _order_key(self):
        return 'order' if self.console == 'wing' else f'order_{self.console}'   # each console keeps its own

    def _load_state(self):
        data = self._state_raw
        self.overrides = {k: list(v) for k, v in data.get('overrides', {}).items()} if not self.x32 else {}
        self.order = self.clean_order(data.get(self._order_key, []))

    def _write_state(self):
        try:
            data = dict(self._state_raw)                 # keep the other console's order
            data.update({'overrides': self.overrides if not self.x32 else data.get('overrides', {}),
                         self._order_key: self.order})
            if self.found:                           # remembered as the first place to ask next time
                data.update({'console': self.console, 'console_ip': self.ip})
            self._state_raw = data
            tmp = STATE_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f'[mixer] could not save {STATE_PATH}: {e}', flush=True)

    def _save_state(self):
        self._write_state()
        self.hub.publish({'t': 'ovr', 'v': sorted(self.overrides)})

    # ── channel display order (page only; shared by every device, never sent to the WING) ──
    def clean_order(self, order):
        valid = {f'{k}/{n}' for k, n in self._strips()}
        out = []
        for k in order if isinstance(order, list) else []:
            if isinstance(k, str) and k in valid and k not in out:
                out.append(k)
        return out

    def set_order(self, order):
        with self._ovr_lock:
            self.order = self.clean_order(order)
            self._write_state()
        self.hub.publish({'t': 'order', 'v': self.order})
        return self.order

    # ── console-view layer layouts (v4.0): named profiles kept on the Pi, per console type ──
    def clean_layers(self, layers):
        c = self.caps
        lim = {'ch': c['nch'], 'aux': c['naux'], 'bus': c['nbus'], 'main': c.get('nmain', 2),
               'mtx': c.get('nmtx', 0), 'dca': c.get('ndca', 0), 'fxr': c.get('nfxr', 0)}
        out = []
        for lay in (layers if isinstance(layers, list) else [])[:N_LAYERS]:
            lay = lay if isinstance(lay, dict) else {}
            items = []
            for k in lay.get('items') if isinstance(lay.get('items'), list) else []:
                m = LAYOUT_ITEM.match(str(k))
                if m and 1 <= int(m.group(2)) <= lim[m.group(1)] and k not in items:
                    items.append(k)
                if len(items) >= 128:
                    break
            out.append({'name': str(lay.get('name') or '').strip()[:12], 'items': items})
        while len(out) < N_LAYERS:
            out.append({'name': '', 'items': []})
        return out

    def layouts(self):
        return dict((self._state_raw.get('layouts') or {}).get(self.console) or {})

    def save_layout(self, profile, layers=None, delete=False, email=''):
        name = str(profile or '').strip()[:24]
        if not name:
            return None, 'profile name required'
        with self._ovr_lock:
            allp = dict(self._state_raw.get('layouts') or {})
            mine = dict(allp.get(self.console) or {})
            if delete:
                mine.pop(name, None)
            else:
                if name not in mine and len(mine) >= MAX_PROFILES:
                    return None, 'too many profiles'
                ent = {'layers': self.clean_layers(layers)}
                email = str(email or (mine.get(name) or {}).get('email') or '').strip()[:120]
                if email:
                    ent['email'] = email
                mine[name] = ent
            allp[self.console] = mine
            self._state_raw = dict(self._state_raw, layouts=allp)
            self._write_state()
        self.hub.publish({'t': 'layouts', 'console': self.console, 'v': mine})
        return mine, None

    def _restore_tags(self, b, groups=None):
        """Put removed '#Mn' tags back on strip b (all, or just `groups`). Keeps any other tag edits."""
        with self._ovr_lock:
            removed = self.overrides.get(b, [])
            back = [g for g in removed if groups is None or g in groups]
            if not back:
                return
            cur = self._tag_list(self.wing.query(b + '/tags'))
            missing = [g for g in back if g not in cur]
            if missing:
                self.wing.set(b + '/tags', ','.join(cur + missing))
            left = [g for g in removed if g not in back]
            if left:
                self.overrides[b] = left
            else:
                self.overrides.pop(b, None)
            self._save_state()
        print(f'[mixer] override end {b}: restored {back}', flush=True)
        got = self.wing.query_many([b + '/tags', b + '/$mute'], timeout=0.6)
        for a, v in got.items():
            if v is not None:
                self.hub.publish({'t': 'upd', 'a': a, 'v': v})

    def check_overrides(self):
        """Restore tags for groups that are no longer engaged (released at the console, another app,
        or the page). A tag already back (scene recall) just clears the record."""
        if self.x32:
            return                                    # X32 override is a plain unmute; nothing to restore
        for b, removed in list(self.overrides.items()):
            tags = self._tag_list(self.wing.get(b + '/tags'))
            for g in list(removed):
                if g in tags:                                   # already restored (e.g. scene recall)
                    with self._ovr_lock:
                        rest = [x for x in self.overrides.get(b, []) if x != g]
                        if rest: self.overrides[b] = rest
                        else: self.overrides.pop(b, None)
                        self._save_state()
            released = [g for g in self.overrides.get(b, []) if not self.wing.get(f'/mgrp/{g[2:]}/mute')]
            if released:
                self._restore_tags(b, released)

    def toggle_mute(self, kind, n):
        """Console MUTE-button semantics:
        overridden here          -> put the group tags back (group-muted again)
        $mute 2 (group-muted)    -> remove the engaged groups' tags (override), verify it unmuted
        otherwise                -> toggle the strip's own 'mute'."""
        b = f'/{kind}/{n}'
        if self.x32:
            ok, action, st = self.wing.toggle_mute(kind, n)
            if action == 'override':
                print(f'[mixer] mute {b}: group override -> $mute {st}', flush=True)
            return ok, action, st
        if b in self.overrides:
            self._restore_tags(b)
            return True, 'regroup', self.wing.get(b + '/$mute')
        got = self.wing.query_many([b + '/$mute', b + '/mute', b + '/tags']
                                   + [f'/mgrp/{g}/mute' for g in range(1, 9)], timeout=0.6)
        cur, own = got.get(b + '/$mute'), got.get(b + '/mute')
        tags = self._tag_list(got.get(b + '/tags'))
        engaged = [t for t in tags if re.fullmatch(r'#M[1-8]', t) and got.get(f'/mgrp/{t[2:]}/mute')]
        if cur == 2 and not own and engaged:
            with self._ovr_lock:
                self.wing.set(b + '/tags', ','.join(t for t in tags if t not in engaged))
                time.sleep(0.25)
                after = self.wing.query(b + '/$mute')
                if after != 0:                                  # didn't unmute: undo, report
                    self.wing.set(b + '/tags', ','.join(tags))
                    ok, action = False, 'override-failed'
                else:
                    self.overrides[b] = engaged; self._save_state()
                    ok, action = True, 'override'
            print(f'[mixer] override {b}: removed {engaged} -> {action}', flush=True)
        else:
            self.wing.set(b + '/mute', 0 if own else 1)
            ok, action = True, 'own'
        time.sleep(0.08)
        after = self.wing.query_many([b + '/$mute', b + '/mute', b + '/tags'], timeout=0.6)
        for a, v in after.items():
            if v is not None:
                self.hub.publish({'t': 'upd', 'a': a, 'v': v})
        return ok, action, after.get(b + '/$mute')

    def assign(self, kind, n, grp, idx, on):
        """v4.0.3: put a strip in / take it out of mute group (grp 'M') or DCA ('D') idx."""
        b = f'/{kind}/{n}'
        if self.x32:
            return self.wing.assign(kind, n, grp, idx, on)
        tag = f'#{grp}{idx}'
        with self._ovr_lock:
            if grp == 'M' and b in self.overrides:
                return None, 'pulled out of a mute group here -- press MUTE to put it back first'
            tags = self._tag_list(self.wing.query(b + '/tags'))
            if on and tag not in tags:
                tags.append(tag)
            elif not on:
                tags = [t for t in tags if t != tag]
            v = self.wing.set(b + '/tags', ','.join(tags))
        self.hub.publish({'t': 'upd', 'a': b + '/tags', 'v': v})
        if grp == 'M':
            self.refresh_mutes()
        print(f'[mixer] {b} {"+" if on else "-"}{tag} from {_who()}', flush=True)
        return v, None

    def _source_addrs(self, extra=()):
        srcs = set(extra)
        for k, n in self._strips():
            g, i = self.wing.get(f'/{k}/{n}/in/conn/grp'), self.wing.get(f'/{k}/{n}/in/conn/in')
            if g in SRC_COUNT and isinstance(i, int) and 1 <= i <= SRC_COUNT[g]:
                srcs.add((g, i))
        return [f'/io/in/{g}/{i}/{leaf}' for g, i in sorted(srcs) for leaf in SRC_LEAVES]

    def src_groups(self):
        return x32mod.PICK_GROUPS if self.x32 else SRC_GROUPS

    def patch(self, kind, n, grp, idx):
        """Re-patch a channel/aux input. grp first, then in -- then read everything back."""
        if self.x32:
            ok = self.wing.patch_source(kind, n, grp, idx)
            print(f'[mixer] patched {kind} {n} -> {grp} {idx} ({"ok" if ok else "NOT confirmed"})', flush=True)
            if kind == 'ch' and n == self.ambient_channel():
                threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
            return ok
        base = f'/{kind}/{n}/in/conn'
        self.wing.set(base + '/grp', grp); time.sleep(0.03)
        self.wing.set(base + '/in', idx); time.sleep(0.05)
        got = self.wing.query_many([base + '/grp', base + '/in']
                                   + [f'/io/in/{grp}/{idx}/{leaf}' for leaf in SRC_LEAVES], timeout=1.0)
        for a, v in got.items():
            if v is not None:
                self.hub.publish({'t': 'upd', 'a': a, 'v': v})
        if kind == 'ch' and n == self.ambient_channel():
            threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
        print(f'[mixer] patched {kind} {n} -> {grp} {idx}', flush=True)
        return got.get(base + '/grp') == grp and got.get(base + '/in') == idx

    # ── processing nodes (EQ / gate / dyn): driven by the console's own '#' description ──
    def node(self, path):
        if self.x32:
            return self.wing.node(path)
        params = parse_describe(self.wing.describe(path))
        if params:
            self.node_cache[path] = params
        return params

    def node_set(self, path, key, value):
        if self.x32:
            return self.wing.node_set(path, key, value)
        params = self.node_cache.get(path) or self.node(path)
        p = next((x for x in params if x['key'] == key), None)
        if not p or p['ro'] or key in NODE_LOCKED:
            return None, 'parameter not writable'
        t = p['type']
        if t == 'list':
            v = str(value).strip()
            if v not in p['opts']:
                return None, 'not an option'
            self.wing._send(f'{path}/{key}', v)
        elif t == 'int':
            v = int(max(p['lo'], min(p['hi'], round(float(value)))))
            self.wing._send(f'{path}/{key}', v)
        elif t in ('lin', 'log'):
            v = round(max(p['lo'], min(p['hi'], float(value))), 3)
            self.wing._send(f'{path}/{key}', float(v))
        else:
            return None, 'unsupported type'
        p['value'] = v
        return v, None

    def source_names(self, grp):
        if self.x32:
            return self.wing.source_names(grp)
        n = SRC_COUNT[grp]
        got = self.wing.query_many([f'/io/in/{grp}/{i}/{leaf}' for i in range(1, n + 1) for leaf in ('name', 'mode')],
                                   timeout=1.5)
        return [{'n': i, 'name': got.get(f'/io/in/{grp}/{i}/name') or '', 'mode': got.get(f'/io/in/{grp}/{i}/mode') or ''}
                for i in range(1, n + 1)]

    def _meter_pump(self):
        """~10 Hz meter push to page clients. c/a = [[in, out], ...] per ch/aux; b/m = out."""
        while True:
            time.sleep(0.1)
            lv = self.meters.levels
            if not lv or not self.hub.subs:
                continue
            self.hub.publish({'t': 'm',
                              'c': [r[:2] for r in lv['ch']], 'a': [r[:2] for r in lv['aux']],
                              # gate key dB, gate GR %, dyn key dB, dyn GR %
                              'cd': [r[2:] for r in lv['ch']], 'ad': [r[2:] for r in lv['aux']],
                              'b': [r[1] for r in lv['bus']], 'm': [r[1] for r in lv['main']],
                              'x': [r[1] for r in lv.get('mtx') or []],
                              # v4.0.3: dyn key dB / GR % for the output strips (bus / main / mtx detail pages)
                              'bd': [r[4:6] for r in lv['bus']], 'md': [r[4:6] for r in lv['main']],
                              'xd': [r[4:6] for r in lv.get('mtx') or []],
                              'f': [r[1] for r in lv.get('fxr') or []]})

    # ── WING callbacks ──
    def _on_update(self, addr, v):
        if addr.startswith('/cards/wlive/'):
            if addr.endswith(('/etime', '/sdfree')):      # pushed 7-10x/s while recording / playing
                now = time.time()
                wait = 0.5 - (now - self._rec_pub.get(addr, 0))
                if wait > 0:                              # cache is current; publish the latest value at the
                    if addr not in self._rec_tail:        # end of the window, so the final position (pause,
                        self._rec_tail.add(addr)          # marker jump) is never the one that gets dropped
                        threading.Timer(wait, self._rec_flush, (addr,)).start()
                    return
                self._rec_pub[addr] = now
            elif addr.endswith(('/markers', '/sessions', '/state', '/sessionpos')) and self.wing.loaded:
                self.refresh_markers(int(addr.split('/')[3]), delay=0.3)
        self.hub.publish({'t': 'upd', 'a': addr, 'v': v})
        if not self.wing.loaded:
            return                                   # initial load: one snapshot at the end
        if self.x32:                                 # X32 pushes re-patches: keep the ambient User Out on its channel
            follow = self.ambient_channel()
            if follow and addr.startswith(f'/ch/{follow}/in/conn/'):
                threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
            if follow and addr == f'/ch/{follow}/name':        # label carries the channel name
                threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
            elif addr.startswith('/main/') and addr.endswith('name'):
                self.hub.publish({'t': 'feeds', 'feeds': self.feeds()})
            return
        follow = self.ambient_channel()
        if addr.startswith('/io/out/USB/') or (follow and addr.startswith(f'/ch/{follow}/in/conn/')):
            threading.Thread(target=self.ensure_patch, daemon=True).start()
        if addr.endswith('name'):
            self.hub.publish({'t': 'feeds', 'feeds': self.feeds()})

    def _rec_flush(self, addr):
        self._rec_tail.discard(addr)
        self._rec_pub[addr] = time.time()
        self.hub.publish({'t': 'upd', 'a': addr, 'v': self.wing.get(addr)})

    def _on_conn(self, ok):
        self.hub.publish({'t': 'conn', 'ok': ok})

    def _on_loaded(self):
        if self.x32:
            st = self.wing.snapshot()
            print(f"[mixer] {self.caps['model']} loaded: {len(st)} values", flush=True)
            if st.get('/cards/$type') == 'WLIVE':
                self.refresh_markers(1, publish=False)
            self.hub.publish({'t': 'snap', **self.snapshot()})
            threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
            return
        self.wing.query_many(self._source_addrs(), timeout=1.5)     # physical-input settings
        if self.overrides:                                          # after a restart / reconnect
            self.wing.query_many([b + '/tags' for b in self.overrides]
                                 + [f'/mgrp/{g}/mute' for g in range(1, 9)], timeout=1.0)
            self.check_overrides()
        for n in range(1, self.n_sd + 1):
            self.refresh_markers(n, publish=False)
        self.hub.publish({'t': 'snap', **self.snapshot()})
        self.ensure_patch(force=True)

    # ── feeds ──
    def _name(self, addr, fallback):
        """Displayed name: prefer '$name' (what the console shows), then stored 'name'."""
        for a in (addr.replace('/name', '/$name'), addr):
            n = self.wing.get(a)
            if isinstance(n, str) and n.strip():
                return n
        return fallback

    def feeds(self):
        if not self.caps['listen']:
            return []
        out = []
        if self.x32:
            labels = {'main1': f"Main {self._name('/main/1/name', 'LR')} (Out 15/16)",
                      'main2': 'Sub (Out 14 \u00b7 mono)',
                      'mon1': 'Monitor (phones / solo)'}
            for fid, ul, ur, *_ in feed_table('x32'):
                out.append({'id': fid, 'label': labels[fid], 'usb': [ul, ur]})
            out.append({'id': 'ambient', 'label': self.ambient_label, 'usb': [X32_AMBIENT_USB, X32_AMBIENT_USB]})
            return out
        for fid, ul, ur, grp, il, ir in feed_table('wing'):
            if fid == 'main1':
                label = f"Main {self._name('/main/1/name', 'LR')}"
            elif fid == 'main2':
                label = f"Main 2 \u2013 {self._name('/main/2/name', 'Subs')}"
            elif fid == 'mon1':
                label = 'Monitor 1 (phones / solo)'
            elif fid.startswith('bus'):
                b = fid[3:]; label = f"Bus {b} \u2013 {self._name(f'/bus/{b}/name', '')}".rstrip(' \u2013')
            else:
                m = fid[3:]; label = f"Mtx {m} \u2013 {self._name(f'/mtx/{m}/name', '')}".rstrip(' \u2013')
            out.append({'id': fid, 'label': label, 'usb': [ul, ur]})
        out.append({'id': 'ambient', 'label': self.ambient_label, 'usb': [AMBIENT_USB, AMBIENT_USB]})
        return out

    def _blend_for(self, fid, feeds):
        """Sub blend applies to the main LR feed only, when Main 2 / the sub feed exists."""
        if fid != 'main1' or not self.sub_on:
            return None
        sub = next((f for f in feeds if f['id'] == 'main2'), None)
        if not sub:
            return None
        return (sub['usb'][0] - 1, sub['usb'][1] - 1, 10 ** (self.sub_db / 20.0))

    def select_feed(self, fid, publish=True):
        feeds = self.feeds()
        for f in feeds:
            if f['id'] == fid:
                self.feed_id = fid
                self.listener.set_route(f['usb'][0] - 1, f['usb'][1] - 1, self._blend_for(fid, feeds),
                                        info={'on': self.sub_on, 'db': self.sub_db})
                if publish:
                    self.hub.publish({'t': 'feed', 'id': fid})
                return True
        return False

    # ── USB patch ownership ──
    def _ambient_source(self):
        ch = self.ambient_channel()
        if ch:
            g = self.wing.query(f'/ch/{ch}/in/conn/grp')
            i = self.wing.query(f'/ch/{ch}/in/conn/in')
            if isinstance(g, str) and g and g != 'OFF' and isinstance(i, int) and i > 0:
                return g, i, f'follows Ch {ch}'
        if self.x32:
            return None, None, f'Ch {ch} has no physical input' if ch else 'no channel chosen'
        amb = self.cfg.get('ambient') or {}
        return amb.get('grp', 'B'), int(amb.get('in', 4)), 'fixed'

    def _set_ambient_label(self, g, i):
        ch = self.ambient_channel()
        lab = 'Ambient'
        if ch:
            nm = self._name(f'/ch/{ch}/name', '')
            lab += f' \u00b7 Ch {ch}' + (f' {nm.strip()}' if isinstance(nm, str) and nm.strip() else '')
        if g:
            lab += f' ({SRC_NAMES.get(g, g)} {i})'
        elif ch:
            lab += ' (no input)'
        if lab != self.ambient_label:
            self.ambient_label = lab
            self.hub.publish({'t': 'feeds', 'feeds': self.feeds()})

    def ensure_patch(self, force=False):
        """Make the console's listen outputs match feed_table + ambient. Writes only what differs.
        WING: USB outs. X32: User Out 3-8 + the Card out 25-32 block (blocks 1-24 are never touched)."""
        if not self.cfg.get('usb_patch', True) or not self.wing.connected:
            return
        if self.x32:
            return self._ensure_x32_patch(force)
        with self._patch_lock:
            if not force and time.time() - self._last_patch < 10:   # never fight a recall loop
                return
            self._last_patch = time.time()
            want = []
            for _, ul, ur, grp, il, ir in feed_table('wing'):
                want += [(ul, grp, il), (ur, grp, ir)]
            ag, ai, how = self._ambient_source()
            self._set_ambient_label(ag, ai)
            want.append((AMBIENT_USB, ag, ai))
            fixed = 0
            for usb, grp, idx in want:
                base = f'/io/out/USB/{usb}'
                cur_g = self.wing.query(base + '/grp')
                cur_i = self.wing.query(base + '/in')
                if cur_g != grp:
                    self.wing.set(base + '/grp', grp); time.sleep(0.02); fixed += 1
                if cur_i != idx or cur_g != grp:
                    self.wing.set(base + '/in', idx); time.sleep(0.02); fixed += 1
            self.patch_note = (f'USB patch OK ({fixed} writes); ambient {ag} {ai} ({how})')
            print(f'[mixer] {self.patch_note}', flush=True)
            self.hub.publish({'t': 'patch', 'note': self.patch_note})

    def _ensure_x32_patch(self, force=False):
        with self._patch_lock:
            if not force and time.time() - self._last_patch < 10:
                return
            self._last_patch = time.time()
            ag, ai, how = self._ambient_source()
            self._set_ambient_label(ag, ai)
            want = {f'/config/userrout/out/{slot:02d}': (x32_input_code(ag, ai) if key == 'ambient' else X32_UOUT[key])
                    for slot, key in X32_UOUT_SLOTS.items()}
            card = f'/config/routing/CARD/{X32_CARD_BLOCK}'
            want[card] = X32_CARD_UOUT18
            cur = self.wing._query_raw(list(want), timeout=0.8)
            fixed = []
            for addr, v in want.items():
                if cur.get(addr) != v:
                    self.wing._write(addr, v); time.sleep(0.02); fixed.append(addr.rsplit('/', 1)[1])
            if fixed:
                got = self.wing._query_raw([a for a in want if a.rsplit('/', 1)[1] in fixed], timeout=0.8)
                bad = [a for a in got if got[a] != want[a]]
            else:
                bad = []
            note = (f'X32 listen block {"OK" if not bad else "NOT confirmed"} ({len(fixed)} writes): '
                    f'card 25-32 = User Out 1-8; UO3 ambient '
                    + (f'{SRC_NAMES.get(ag, ag)} {ai}' if ag else 'off') + f' ({how})')
            if bad:
                note += ' -- console did not take ' + ', '.join(a.rsplit('/', 2)[-2] + '/' + a.rsplit('/', 1)[1] for a in bad)
            if fixed or note != self.patch_note:
                print(f'[mixer] {note}' + (f' [wrote {", ".join(fixed)}]' if fixed else ''), flush=True)
            if note != self.patch_note:
                self.patch_note = note
                self.hub.publish({'t': 'patch', 'note': note})

    # ── snapshot ──
    # ── WING-LIVE SD recorder ──
    # The marker and session *lists* only exist as the option lists of $stat/markerlist and
    # $stat/sessionlist (a push carries just the selected entry), so they are read from the node
    # description whenever the count / state / open session changes.
    def refresh_markers(self, card, delay=0.0, publish=True):
        def go():
            if delay:
                time.sleep(delay)
            if self.x32:
                marks, sess = self.wing.rec_lists()
                self._set_lists(card, marks, sess, publish)
                return
            txt = self.wing.describe(f'/cards/wlive/{card}/$stat')
            if txt is None:
                return
            ps = {x['key']: x for x in parse_describe(txt)}
            marks = [m for m in ps.get('markerlist', {}).get('opts', []) if m]
            sess = [x for x in ps.get('sessionlist', {}).get('opts', []) if x]
            if marks != self.rec_markers.get(card):
                self.rec_markers[card] = marks
                if publish:
                    self.hub.publish({'t': 'recm', 'c': card, 'v': marks})
            if sess != self.rec_sessions.get(card):
                self.rec_sessions[card] = sess
                if publish:
                    self.hub.publish({'t': 'recs', 'c': card, 'v': sess})
        if publish:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()

    def _set_lists(self, card, marks, sess, publish):
        if marks != self.rec_markers.get(card):
            self.rec_markers[card] = marks
            if publish:
                self.hub.publish({'t': 'recm', 'c': card, 'v': marks})
        if sess != self.rec_sessions.get(card):
            self.rec_sessions[card] = sess
            if publish:
                self.hub.publish({'t': 'recs', 'c': card, 'v': sess})

    def rec_state(self, card):
        return self.wing.get(f'/cards/wlive/{card}/$stat/state')

    def after_open(self, card):
        """opensession pushes little: re-read the open session's length/position/markers twice."""
        b = f'/cards/wlive/{card}/$stat'
        addrs = [f'{b}/{k}' for k in ('sessionpos', 'sessionlen', 'markers', 'markerpos', 'etime', 'state')]
        def go():
            for d in (0.8, 1.7):
                time.sleep(d)
                self.wing.query_many(addrs, timeout=0.8)
                self.refresh_markers(card, publish=True)
        threading.Thread(target=go, daemon=True).start()

    # ── WebRTC listen-back (MediaMTX) ──
    def rtc_probe(self):
        """(api_ok, stream_ready, reader_count) for the MediaMTX 'listen' path."""
        try:
            with urllib.request.urlopen(self.cfg['rtc']['api'] + '/v3/paths/get/listen', timeout=0.6) as r:
                p = json.load(r)
            return True, bool(p.get('ready')), len(p.get('readers') or [])
        except urllib.error.HTTPError:
            return True, False, 0          # MediaMTX answered: path just isn't publishing
        except Exception:
            return False, False, 0

    def cam_probe(self):
        """(api_ok, stream_ready, reader_count) for the MediaMTX 'cam' path."""
        try:
            with urllib.request.urlopen(self.cfg['rtc']['api'] + '/v3/paths/get/cam', timeout=0.6) as r:
                p = json.load(r)
            return True, bool(p.get('ready')), len(p.get('readers') or [])
        except urllib.error.HTTPError:
            return True, False, 0
        except Exception:
            return False, False, 0

    def save_listen_bitrate(self, rate):
        """The Listen card's low-latency (Opus) bitrate survives restarts (v3.5)."""
        with self._ovr_lock:
            self._state_raw = {**self._state_raw, 'listen': {**(self._state_raw.get('listen') or {}), 'opus_bitrate': rate}}
            self._write_state()

    def _save_cam(self, d):
        """Page-made video settings (audio feed, delay, quality) survive restarts."""
        with self._ovr_lock:
            self._state_raw = {**self._state_raw, 'cam': dict(d)}
            self._write_state()

    def ice_servers(self):
        """ICE servers for browsers: Cloudflare TURN (cached short-lived credentials) when a key is
        configured, else STUN only (fine on the same network, usually not across the internet)."""
        stun = [{'urls': ['stun:stun.cloudflare.com:3478']}]
        rtc = self.cfg['rtc']
        kid, tok, ttl = rtc.get('turn_key_id'), rtc.get('turn_api_token'), int(rtc.get('turn_ttl') or 86400)
        if not (kid and tok):
            return stun, False
        with self._turn_lock:
            now = time.time()
            if self._turn and now < self._turn_exp - ttl / 2:
                return self._turn, True
            try:
                req = urllib.request.Request(
                    f'https://rtc.live.cloudflare.com/v1/turn/keys/{kid}/credentials/generate-ice-servers',
                    data=json.dumps({'ttl': ttl}).encode(), method='POST',
                    headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json',
                             'User-Agent': 'stage-messenger'})
                with urllib.request.urlopen(req, timeout=6) as r:
                    servers = json.load(r).get('iceServers') or []
                # Keep it to four URLs (browsers slow down with more): STUN, TURN/UDP, TURN/TCP,
                # and TURN over TLS 443 for networks that block everything else.
                keep = ('stun:', '3478?transport=udp', '3478?transport=tcp', 'turns:turn.cloudflare.com:443')
                out = []
                for s in servers:
                    urls = [u for u in ([s['urls']] if isinstance(s.get('urls'), str) else s.get('urls', []))
                            if any(k in u for k in keep)]
                    if urls:
                        out.append({**s, 'urls': urls})
                self._turn, self._turn_exp = out or servers, now + ttl
                return self._turn, True
            except Exception as e:
                print(f'[mixer] TURN credentials failed: {e}', flush=True)
                if self._turn and now < self._turn_exp:
                    return self._turn, True
                return stun, False

    def snapshot(self):
        return {
            'conn':   self.wing.connected,
            'loaded': self.wing.loaded,
            'found':  self.found or self.wing.connected,
            'ip':     self.ip,
            'state':  self.wing.snapshot(),
            'feeds':  self.feeds(),
            'feed':   self.feed_id,
            'ambient_ch': self.ambient_channel(),
            'listen': self.listener.status(),
            'cam':    self.cam.status(),
            'patch':  self.patch_note,
            'nbus':   self.caps['nbus'],
            'caps':   self.caps,
            'meters': self.meters.levels is not None,
            'srcgroups': self.src_groups(),
            'ovr':    sorted(self.overrides),
            'order':  self.order,
            'listen_target': self.cfg.get('listen_target_s', 0.8),
            'recm':   self.rec_markers,
            'recs':   self.rec_sessions,
            'sp':     self.spotify.snapshot() if self.spotify else None,
            'layouts': self.layouts(),
        }


# ── Blueprint ───────────────────────────────────────────────────────────────────

bp = Blueprint('mixer', __name__, url_prefix='/mixer')
_mixer = None


@bp.before_request
def _guard():
    # cloudflared adds Cf-Connecting-Ip to every request that came through the tunnel.
    if request.headers.get('Cf-Connecting-Ip'):
        if not _mixer.cfg.get('remote_enabled'):
            abort(403, 'Remote mixer access is disabled (set remote_enabled in mixer_config.json).')
        if not request.headers.get('Cf-Access-Jwt-Assertion'):
            abort(403, 'Cloudflare Access is not protecting /mixer.')


@bp.after_request
def _log_rejects(resp):
    # The werkzeug filter in init_mixer hides successful control traffic; rejected requests land here.
    if resp.status_code >= 400:
        who = request.headers.get('Cf-Connecting-Ip')
        src = f"tunnel {who} (Access JWT {'yes' if request.headers.get('Cf-Access-Jwt-Assertion') else 'NO'})" \
            if who else f'local {request.remote_addr}'
        print(f'[mixer] REJECTED {resp.status_code} {request.method} {request.path} from {src}', flush=True)
    return resp


def _not_here(feature):
    """409 for features the connected console's driver doesn't do yet (None when it does)."""
    if feature == 'sheet' and not _mixer.caps['sheet'] or feature == 'recorder' and not _mixer.caps['recorder'] \
            or feature == 'listen' and not _mixer.caps['listen']:
        return jsonify(ok=False, err=f"not available on the {_mixer.caps['model']} yet"), 409
    return None


@bp.route('', strict_slashes=False)
def page():
    # The page builds its strips before the first snapshot arrives, so it gets the console's
    # capabilities up front (a later snapshot with different caps makes it reload).
    with open(os.path.join(HERE, 'mixer.html'), encoding='utf-8') as f:
        html = f.read()
    caps = json.dumps(_mixer.caps, separators=(',', ':')).replace('</', '<\\/')
    html = html.replace('const CAPS = null; /*CAPS*/', f'const CAPS = {caps}; /*CAPS*/', 1)
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _email():
    """Cloudflare Access user (remote requests only) -- picks a console-view layout profile."""
    return request.headers.get('Cf-Access-Authenticated-User-Email', '') if request.headers.get('Cf-Connecting-Ip') else ''


@bp.route('/api/state')
def api_state():
    return jsonify({**_mixer.snapshot(), 'who': _email()})


@bp.route('/api/events')
def api_events():
    q = _mixer.hub.subscribe()
    who = _email()

    def gen():
        try:
            yield 'retry: 2000\n\n'
            yield 'data: ' + json.dumps({'t': 'snap', **_mixer.snapshot(), 'who': who}, separators=(',', ':')) + '\n\n'
            while True:
                try:
                    yield 'data: ' + q.get(timeout=15) + '\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            _mixer.hub.unsubscribe(q)

    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/api/set', methods=['POST'])
def api_set():
    d = request.get_json(silent=True) or {}
    addr = str(d.get('a', ''))
    if not SETTABLE.match(addr):
        return jsonify(ok=False, err='address not allowed'), 400
    try:
        v = _mixer.wing.set(addr, d.get('v'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad value'), 400
    if v is None:
        return jsonify(ok=False, err='bad value'), 400
    _mixer.hub.publish({'t': 'upd', 'a': addr, 'v': v})
    if addr.startswith(LOGGED_SETS):
        print(f'[mixer] {addr} = {v} from {_who()}', flush=True)
    if addr.startswith('/mgrp/'):
        _mixer.refresh_mutes()            # members' $mute changes silently
        if not v:                         # group released -> put back any overridden members now
            threading.Thread(target=lambda: (time.sleep(0.3), _mixer.check_overrides()), daemon=True).start()
    return jsonify(ok=True, v=v)


@bp.route('/api/feed', methods=['POST'])
def api_feed():
    d = request.get_json(silent=True) or {}
    return jsonify(ok=_mixer.select_feed(str(d.get('id', ''))))


@bp.route('/api/patch', methods=['POST'])
def api_patch():
    if _not_here('sheet'):
        return _not_here('sheet')
    d = request.get_json(silent=True) or {}
    kind, grp = str(d.get('kind', '')), str(d.get('grp', ''))
    try:
        n, idx = int(d.get('n')), int(d.get('in'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad number'), 400
    lim = {'ch': _mixer.caps['nch'], 'aux': _mixer.caps['naux']}.get(kind)
    counts = dict(_mixer.src_groups())
    if not lim or not 1 <= n <= lim or grp not in counts or not 1 <= idx <= counts[grp]:
        return jsonify(ok=False, err='bad patch target'), 400
    return jsonify(ok=_mixer.patch(kind, n, grp, idx))


@bp.route('/api/srcnames')
def api_srcnames():
    if _not_here('sheet'):
        return _not_here('sheet')
    grp = request.args.get('g', '')
    if grp not in dict(_mixer.src_groups()):
        return jsonify(ok=False, err='bad group'), 400
    return jsonify(ok=True, g=grp, inputs=_mixer.source_names(grp))


@bp.route('/api/node')
def api_node():
    if _not_here('sheet'):
        return _not_here('sheet')
    path = request.args.get('path', '')
    if not _node_ok(path):
        return jsonify(ok=False, err='bad node'), 400
    params = _mixer.node(path)
    return jsonify(ok=bool(params), path=path, params=params)


@bp.route('/api/nodeset', methods=['POST'])
def api_nodeset():
    if _not_here('sheet'):
        return _not_here('sheet')
    d = request.get_json(silent=True) or {}
    path, key = str(d.get('path', '')), str(d.get('key', ''))
    if not _node_ok(path):
        return jsonify(ok=False, err='bad node'), 400
    try:
        v, err = _mixer.node_set(path, key, d.get('value'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad value'), 400
    if err:
        return jsonify(ok=False, err=err), 400
    return jsonify(ok=True, value=v)


@bp.route('/api/mute', methods=['POST'])
def api_mute():
    d = request.get_json(silent=True) or {}
    kind = str(d.get('kind', ''))
    try:
        n = int(d.get('n'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad number'), 400
    lim = {'ch': _mixer.caps['nch'], 'aux': _mixer.caps['naux']}.get(kind)
    if not lim or not 1 <= n <= lim:
        return jsonify(ok=False, err='bad strip'), 400
    ok, action, state = _mixer.toggle_mute(kind, n)
    return jsonify(ok=ok, action=action, state=state)


@bp.route('/api/assign', methods=['POST'])
def api_assign():
    d = request.get_json(silent=True) or {}
    kind, grp = str(d.get('kind', '')), str(d.get('grp', ''))
    try:
        n, idx = int(d.get('n')), int(d.get('idx'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad number'), 400
    lim = {'ch': _mixer.caps['nch'], 'aux': _mixer.caps['naux'], 'bus': _mixer.caps['nbus'],
           'fxr': _mixer.caps.get('nfxr', 0)}.get(kind)
    top = {'M': _mixer.caps['nmg'], 'D': _mixer.caps.get('ndca', 0)}.get(grp)
    if not lim or not 1 <= n <= lim or not top or not 1 <= idx <= top:
        return jsonify(ok=False, err='bad assignment'), 400
    v, err = _mixer.assign(kind, n, grp, idx, bool(d.get('on')))
    if err:
        return jsonify(ok=False, err=err), 409
    return jsonify(ok=True, tags=v)


@bp.route('/api/order', methods=['POST'])
def api_order():
    d = request.get_json(silent=True) or {}
    if not isinstance(d.get('order'), list):
        return jsonify(ok=False, err='order must be a list'), 400
    return jsonify(ok=True, order=_mixer.set_order(d['order']))


@bp.route('/api/layouts', methods=['GET', 'POST'])
def api_layouts():
    if request.method == 'GET':
        return jsonify(ok=True, console=_mixer.console, profiles=_mixer.layouts(), who=_email())
    d = request.get_json(silent=True) or {}
    prof, err = _mixer.save_layout(d.get('profile'), d.get('layers'), bool(d.get('delete')), _email())
    if err:
        return jsonify(ok=False, err=err), 400
    return jsonify(ok=True, profiles=prof)


@bp.route('/api/repatch', methods=['POST'])
def api_repatch():
    if _not_here('listen'):
        return _not_here('listen')
    threading.Thread(target=_mixer.ensure_patch, kwargs={'force': True}, daemon=True).start()
    return jsonify(ok=True)


@bp.route('/stream.mp3')
def stream():
    if _not_here('listen'):
        return _not_here('listen')
    threading.Thread(target=_mixer.ensure_patch, daemon=True).start()
    q = _mixer.listener.add_client(request.args.get('id', ''))

    def gen():
        try:
            while True:
                try:
                    chunk = q.get(timeout=5)
                except queue.Empty:
                    continue
                if chunk is None:
                    return
                yield chunk
        finally:
            _mixer.listener.remove_client(q)

    return Response(gen(), mimetype='audio/mpeg',
                    headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'})


def _who():
    ip = request.headers.get('Cf-Connecting-Ip')
    return f'tunnel {ip}' if ip else f'local {request.remote_addr}'


@bp.route('/api/rec', methods=['POST'])
def api_rec():
    """WING-LIVE recorder: {action: rec|stop|marker, card: 1|2|'all'}. 'all' (markers only) marks
    every card that is recording; if none is, every card that is playing or paused (marker at the
    play head). Every press is logged -- it's the show recording."""
    if _not_here('recorder'):
        return _not_here('recorder')
    d = request.get_json(silent=True) or {}
    action, card = str(d.get('action', '')), d.get('card')
    playback = False
    if card == 'all' and action == 'marker':
        cards = [n for n in range(1, _mixer.n_sd + 1) if _mixer.rec_state(n) == 'REC']
        if not cards:
            cards = [n for n in range(1, _mixer.n_sd + 1) if _mixer.rec_state(n) in ('PLAY', 'PPAUSE')
                     and _mixer.wing.get(f'/cards/wlive/{n}/$stat/sdstate') == 'READY']
            playback = True
        if not cards:
            return jsonify(ok=False, err='nothing is recording or playing'), 409
    else:
        try:
            cards = [int(card)]
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad card'), 400
        if not 1 <= cards[0] <= _mixer.n_sd:
            return jsonify(ok=False, err='bad card'), 400
    if action not in ('rec', 'stop', 'marker'):
        return jsonify(ok=False, err='bad action'), 400
    if not _mixer.wing.connected:
        return jsonify(ok=False, err=f"{_mixer.caps['model']} offline"), 503
    for n in cards:
        base = f'/cards/wlive/{n}'
        if action == 'rec':
            if _mixer.rec_state(n) in ('PLAY', 'PPAUSE'):
                return jsonify(ok=False, err=f'card {"AB"[n - 1]} is playing back -- stop it first'), 409
            sd = _mixer.wing.get(f'{base}/$stat/sdstate')
            if sd != 'READY':
                return jsonify(ok=False, err=f'card {"AB"[n - 1]} is {sd or "not ready"}'), 409
            _mixer.wing.set(f'{base}/$ctl/control', 'REC')
        elif action == 'stop':
            _mixer.wing.set(f'{base}/$ctl/control', 'STOP')
        else:
            if not playback and _mixer.rec_state(n) != 'REC':
                return jsonify(ok=False, err=f'card {"AB"[n - 1]} is not recording'), 409
            if playback and int(_mixer.wing.get(f'{base}/$stat/markers') or 0) >= 100:
                return jsonify(ok=False, err=f'card {"AB"[n - 1]}: session already has 100 markers'), 409
            _mixer.wing.set(f'{base}/$ctl/setmarker', 1)
            if playback:
                for dl in (0.4, 1.5):
                    _mixer.refresh_markers(n, delay=dl)
    print(f'[mixer] recorder: {action.upper()} card {"+".join("AB"[n - 1] for n in cards)}'
          f'{" (playback)" if playback else ""} from {_who()}', flush=True)
    return jsonify(ok=True, cards=cards, playback=playback)


PLAY_ACTIONS = ('open', 'play', 'pause', 'stop', 'goto', 'seek', 'mark', 'movemark', 'delmark')
_MARK_T = re.compile(r'^(\d+):(\d+):(\d+(?:\.\d+)?)$')


def _mark_ms(txt):
    """'00:13:53.23' -> 833230.0 (None if unparseable)."""
    m = _MARK_T.match(str(txt).strip())
    return ((int(m.group(1)) * 60 + int(m.group(2))) * 60 + float(m.group(3))) * 1000 if m else None


def _clock(ms):
    t = int(ms // 1000)
    return f'{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}' if t >= 3600 else f'{t // 60}:{t % 60:02d}'


@bp.route('/api/play', methods=['POST'])
def api_play():
    """WING-LIVE playback: {card: 1|2, action: open|play|pause|stop|goto|seek, n, ms}.
    'open n' opens session n (1-based, sessionlist order). 'goto n' jumps to marker n; 'seek ms' moves
    the play head (stime + gotomarker 101, see wing.py) -- both work paused, stopped and playing (a
    marker jump while playing is done as a seek to the marker's time). 'mark' adds a marker at the play
    head, 'movemark n' moves marker n there, 'delmark n' deletes it -- any state but recording; these
    write to the SD card. Every press is logged."""
    if _not_here('recorder'):
        return _not_here('recorder')
    d = request.get_json(silent=True) or {}
    action = str(d.get('action', ''))
    try:
        card = int(d.get('card'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad card'), 400
    if not 1 <= card <= _mixer.n_sd:
        return jsonify(ok=False, err='bad card'), 400
    if action not in PLAY_ACTIONS:
        return jsonify(ok=False, err='bad action'), 400
    if action == 'movemark' and not _mixer.caps.get('movemark'):
        return jsonify(ok=False, err=f"the {_mixer.caps['model']} can't move a marker (delete it and add a new one)"), 409
    n = None
    if action in ('open', 'goto', 'movemark', 'delmark'):
        try:
            n = int(d.get('n'))
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad number'), 400
    elif action == 'seek':
        try:
            n = float(d.get('ms'))
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad time'), 400
        if n != n:                                         # NaN
            return jsonify(ok=False, err='bad time'), 400
    if not _mixer.wing.connected:
        return jsonify(ok=False, err=f"{_mixer.caps['model']} offline"), 503
    w, base, L = _mixer.wing, f'/cards/wlive/{card}', 'AB'[card - 1]
    st = _mixer.rec_state(card) or 'STOP'
    if st == 'REC':
        return jsonify(ok=False, err=f'card {L} is recording'), 409
    sd = w.get(f'{base}/$stat/sdstate')
    if sd != 'READY' and action != 'stop':
        return jsonify(ok=False, err=f'card {L} is {sd or "not ready"}'), 409
    count = lambda k: int(w.get(f'{base}/$stat/{k}') or 0)
    if action == 'open':
        if not 1 <= n <= count('sessions'):
            return jsonify(ok=False, err=f'card {L} has no session {n}'), 409
        if st == 'PLAY':
            return jsonify(ok=False, err='stop playback first'), 409
        if st == 'PPAUSE':
            w.set(f'{base}/$ctl/control', 'STOP'); time.sleep(0.15)
        w.set(f'{base}/$ctl/opensession', n)
        _mixer.after_open(card)
    elif action == 'play':
        if not count('sessions'):
            return jsonify(ok=False, err=f'card {L} has no sessions'), 409
        w.set(f'{base}/$ctl/control', 'PLAY')
    elif action == 'pause':
        if st != 'PLAY':
            return jsonify(ok=False, err=f'card {L} is not playing'), 409
        w.set(f'{base}/$ctl/control', 'PPAUSE')
    elif action == 'stop':
        w.set(f'{base}/$ctl/control', 'STOP')
    elif action == 'goto':
        if not 1 <= n <= count('markers'):
            return jsonify(ok=False, err=f'no marker {n} in this session'), 409
        marks = _mixer.rec_markers.get(card) or []
        t = _mark_ms(marks[n - 1]) if n <= len(marks) else None
        if st == 'PLAY' and t is not None:
            w.seek(card, t)                                # gotomarker N is only proven paused/stopped
        else:
            w.set(f'{base}/$ctl/gotomarker', n)
    elif action in ('mark', 'movemark', 'delmark'):
        if not count('sessions') or float(w.get(f'{base}/$stat/sessionlen') or 0) <= 0:
            return jsonify(ok=False, err=f'no session open on card {L}'), 409
        here = _clock(float(w.get(f'{base}/$stat/etime') or 0))
        if action == 'mark':
            if count('markers') >= 100:
                return jsonify(ok=False, err='this session already has 100 markers'), 409
            w.set(f'{base}/$ctl/setmarker', 1)
            n = None
            what = f' @ {here}'
        else:
            if not 1 <= n <= count('markers'):
                return jsonify(ok=False, err=f'no marker {n} in this session'), 409
            marks = _mixer.rec_markers.get(card) or []
            was = marks[n - 1] if n <= len(marks) else '?'
            w.set(f'{base}/$ctl/{"editmarker" if action == "movemark" else "deletemarker"}', n)
            what = f' ({was} -> {here})' if action == 'movemark' else f' ({was})'
        for dl in (0.4, 1.5):                       # editmarker pushes nothing useful: re-read the list
            _mixer.refresh_markers(card, delay=dl)  # (publishes only if it changed)
        print(f'[mixer] playback: {action.upper()}{"" if n is None else " " + str(n)}{what} card {L} '
              f'(was {st}) from {_who()}', flush=True)
        return jsonify(ok=True, card=card)
    else:                                                  # seek
        length = float(w.get(f'{base}/$stat/sessionlen') or 0)
        if length <= 0 or not count('sessions'):
            return jsonify(ok=False, err=f'no session open on card {L}'), 409
        n = max(0.0, min(length, n))
        w.seek(card, n)
    shown = '' if n is None else ' ' + (_clock(n) if action == 'seek' else str(n))
    print(f'[mixer] playback: {action.upper()}{shown} card {L} (was {st}) from {_who()}', flush=True)
    return jsonify(ok=True, card=card)


# ── Spotify card (v2.4; renamed from "Pi playback" in v2.5.1): go-librespot "Stage Rig" -- see mixer/spotify.py ──
@bp.route('/api/sp/cmd', methods=['POST'])
def api_sp_cmd():
    """{cmd: playpause|next|prev|seek|shuffle|repeat|disconnect|play|queue, v}. Forwarded to go-librespot.
    play v = {uri: playlist/album/Liked, skip?: track uri, shuffle?: bool}; queue v = track uri."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    d = request.get_json(silent=True) or {}
    cmd, v = str(d.get('cmd', '')), d.get('v')
    if cmd not in sp.CMDS:
        return jsonify(ok=False, err='bad command'), 400
    ok, err = sp.command(cmd, v)
    if cmd in ('disconnect', 'shuffle', 'repeat', 'next', 'prev', 'playpause', 'play', 'queue') or not ok:
        print(f'[mixer] spotify: {cmd.upper()}{"" if v is None else " " + str(v)} from {_who()}'
              f'{"" if ok else " -- REFUSED: " + err}', flush=True)
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, err=err), 409)


@bp.route('/api/sp/playlists')
def api_sp_playlists():
    """Your playlists (go-librespot internal API), Liked Songs first. ?fresh=1 skips the 20 s cache."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    ok, data = sp.playlists(fresh=request.args.get('fresh') == '1')
    return (jsonify(ok=True, **data), 200) if ok else (jsonify(ok=False, err=data), 409)


@bp.route('/api/sp/tracks')
def api_sp_tracks():
    """Songs of a playlist / album / Liked Songs. Poll while ready is false or cached < length."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    ok, data = sp.tracks(request.args.get('uri', ''))
    if ok:
        return jsonify(ok=True, **data)
    return jsonify(ok=False, err=data), (400 if data.startswith('not a') else 409)


@bp.route('/api/sp/search')
def api_sp_search():
    """Spotify search (tracks, albums, playlists) via the user's own developer app."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    ok, data = sp.search(request.args.get('q', ''))
    return (jsonify(ok=True, **data), 200) if ok else (jsonify(ok=False, err=data), 409)


@bp.route('/api/sp/search_creds', methods=['POST'])
def api_sp_search_creds():
    """Renew / set the search key: {id?: client id (blank = keep), secret}. Tested with a real search and
    saved only if it works. The secret is never echoed back or logged."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    d = request.get_json(silent=True) or {}
    ok, msg = sp.set_search_creds(str(d.get('id') or ''), str(d.get('secret') or ''))
    hint = sp.key_info().get('id_hint', '') if ok else ''
    print(f'[mixer] spotify: SEARCH KEY {"saved (client id " + hint + ")" if ok else "NOT saved -- " + msg} '
          f'from {_who()}', flush=True)
    return (jsonify(ok=True, msg=msg, key=sp.key_info()), 200) if ok else (jsonify(ok=False, err=msg), 409)


@bp.route('/api/sp/service', methods=['POST'])
def api_sp_service():
    """Kill switch, service level: {action: restart|repair}. repair = sign out (forget the saved
    login) and restart -> a new pairing code shows on the page."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Spotify is not enabled'), 503
    action = str((request.get_json(silent=True) or {}).get('action', ''))
    if action not in ('restart', 'repair'):
        return jsonify(ok=False, err='bad action'), 400
    print(f'[mixer] spotify: SERVICE {action.upper()} from {_who()}', flush=True)
    ok, err = sp.service(action)
    if not ok:
        print(f'[mixer] spotify: SERVICE {action.upper()} FAILED: {err}', flush=True)
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, err=err), 500)


# ── WebRTC: browsers set up their connection through these (all behind the /mixer guard) ──
_SESSION_ID = re.compile(r'^[0-9a-fA-F-]{36}$')


@bp.route('/api/rtc/config')
def api_rtc_config():
    if not _mixer.rtc_enabled:
        return jsonify(ok=True, enabled=False, why='turned off in mixer_config.json')
    if not _mixer.rtc_probe()[0]:
        return jsonify(ok=True, enabled=False, why='MediaMTX is not running on the Pi')
    ice, turn = _mixer.ice_servers()
    return jsonify(ok=True, enabled=True, iceServers=ice, turn=turn)


@bp.route('/api/rtc/whep', methods=['POST'])
def api_rtc_whep():
    if not _mixer.rtc_enabled or not _mixer.caps['listen']:
        return Response('WebRTC disabled', 404, mimetype='text/plain')
    if (request.content_length or 0) > 65536:
        return Response('offer too large', 413, mimetype='text/plain')
    offer = request.get_data(cache=False)
    if not offer.startswith(b'v=0'):
        return Response('expected an SDP offer', 400, mimetype='text/plain')
    _mixer.listener.hold_rtc(20)               # MediaMTX refuses readers until audio is publishing
    if not _mixer.listener.wait_rtc_ready(6):
        return Response('audio stream not ready', 503, mimetype='text/plain')
    req = urllib.request.Request(_mixer.cfg['rtc']['whep'], data=offer, method='POST',
                                 headers={'Content-Type': 'application/sdp'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            answer, loc = r.read(), r.headers.get('Location', '')
    except urllib.error.HTTPError as e:
        return Response(e.read()[:300], e.code, mimetype='text/plain')
    except Exception as e:
        return Response(f'MediaMTX unreachable: {e}', 502, mimetype='text/plain')
    resp = Response(answer, 201, mimetype='application/sdp')
    m = re.search(r'/whep/([0-9a-fA-F-]{36})', loc)
    if m:
        resp.headers['Location'] = '/mixer/api/rtc/session/' + m.group(1)
    return resp


@bp.route('/api/rtc/session/<sid>', methods=['DELETE'])
def api_rtc_session(sid):
    if not _SESSION_ID.match(sid):
        return Response('bad session', 400, mimetype='text/plain')
    try:
        urllib.request.urlopen(urllib.request.Request(_mixer.cfg['rtc']['whep'] + '/' + sid, method='DELETE'),
                               timeout=3).close()
    except Exception:
        pass                                   # already gone; MediaMTX times sessions out anyway
    return jsonify(ok=True)


# ── Video feed (v3.4): same pattern as listen-back, on MediaMTX's 'cam' path ──
@bp.route('/api/cam/set', methods=['POST'])
def api_cam_set():
    d = request.get_json(silent=True) or {}
    cam = _mixer.cam
    if 'feed' in d and not cam.set_feed(str(d['feed'])):
        return jsonify(ok=False, err='unknown feed'), 400
    if 'delay_ms' in d:
        try:
            cam.set_delay(float(d['delay_ms']))
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad delay'), 400
    if 'quality' in d and not cam.set_quality(str(d['quality'])):
        return jsonify(ok=False, err='unknown quality'), 400
    if 'audio_bitrate' in d and not cam.set_audio_bitrate(str(d['audio_bitrate'])):
        return jsonify(ok=False, err='unknown audio bitrate'), 400
    if 'source' in d and not cam.set_source(str(d['source'])):
        return jsonify(ok=False, err='unknown camera'), 400
    if 'rotate' in d and not cam.set_rotate(str(d['rotate']), str(d.get('rotate_source') or '')):
        return jsonify(ok=False, err='bad rotation'), 400
    return jsonify(ok=True, cam=cam.status())


@bp.route('/api/cam/add', methods=['POST'])
def api_cam_add():
    """Add a Wi-Fi camera (v3.8). The URL stays on the Pi; the page only ever sees the name."""
    d = request.get_json(silent=True) or {}
    cid = _mixer.cam.add_camera(str(d.get('name') or ''), str(d.get('url') or ''))
    if not cid:
        return jsonify(ok=False, err='the address must start with http://, https:// or rtsp:// (max 12 cameras)'), 400
    if d.get('select'):
        _mixer.cam.set_source(cid)
    return jsonify(ok=True, id=cid, cam=_mixer.cam.status())


@bp.route('/api/cam/remove', methods=['POST'])
def api_cam_remove():
    d = request.get_json(silent=True) or {}
    if not _mixer.cam.remove_camera(str(d.get('id') or '')):
        return jsonify(ok=False, err='unknown camera'), 400
    return jsonify(ok=True, cam=_mixer.cam.status())


@bp.route('/api/cam/find', methods=['POST'])
def api_cam_find():
    """Scan the Pi's networks for IP Webcam / DroidCam phones (a few seconds)."""
    if not _mixer.cam.enabled:
        return jsonify(ok=False, err='video is off'), 404
    return jsonify(ok=True, found=_mixer.cam.find_cameras())


@bp.route('/api/listen/set', methods=['POST'])
def api_listen_set():
    """Listen card settings (v3.5): the low-latency Opus bitrate. Restarts the encoder when it changes."""
    if _not_here('listen'):
        return _not_here('listen')
    d = request.get_json(silent=True) or {}
    if 'opus_bitrate' in d:
        rate = str(d['opus_bitrate'])
        if not _mixer.listener.set_opus_bitrate(rate):
            return jsonify(ok=False, err='unknown bitrate'), 400
        _mixer.save_listen_bitrate(rate)
    if 'sub_on' in d or 'sub_db' in d:                 # v3.9 sub blend (shared by every listener)
        try:
            db = float(d['sub_db']) if 'sub_db' in d else None
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad sub_db'), 400
        _mixer.set_sub_blend(on=bool(d['sub_on']) if 'sub_on' in d else None, db=db)
    if 'ambient_ch' in d:                             # v3.9 ambient source = this channel's input
        try:
            ok = _mixer.set_ambient_channel(int(d['ambient_ch']))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            return jsonify(ok=False, err='bad channel'), 400
        _mixer.hub.publish({'t': 'amb', 'ch': _mixer.ambient_channel()})
    return jsonify(ok=True, listen=_mixer.listener.status(), ambient_ch=_mixer.ambient_channel())


@bp.route('/api/cam/whep', methods=['POST'])
def api_cam_whep():
    cam = _mixer.cam
    if not cam.available():
        return Response('no camera available', 404, mimetype='text/plain')
    if (request.content_length or 0) > 65536:
        return Response('offer too large', 413, mimetype='text/plain')
    offer = request.get_data(cache=False)
    if not offer.startswith(b'v=0'):
        return Response('expected an SDP offer', 400, mimetype='text/plain')
    cam.hold(25)                                # starts the encoder; MediaMTX refuses readers until it publishes
    if not cam.wait_ready(12):
        return Response((cam.error or 'video stream not ready')[:200], 503, mimetype='text/plain')
    req = urllib.request.Request(_mixer.cfg['cam']['whep'], data=offer, method='POST',
                                 headers={'Content-Type': 'application/sdp'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            answer, loc = r.read(), r.headers.get('Location', '')
    except urllib.error.HTTPError as e:
        return Response(e.read()[:300], e.code, mimetype='text/plain')
    except Exception as e:
        return Response(f'MediaMTX unreachable: {e}', 502, mimetype='text/plain')
    resp = Response(answer, 201, mimetype='application/sdp')
    m = re.search(r'/whep/([0-9a-fA-F-]{36})', loc)
    if m:
        resp.headers['Location'] = '/mixer/api/cam/session/' + m.group(1)
    return resp


@bp.route('/api/cam/session/<sid>', methods=['DELETE'])
def api_cam_session(sid):
    if not _SESSION_ID.match(sid):
        return Response('bad session', 400, mimetype='text/plain')
    try:
        urllib.request.urlopen(urllib.request.Request(_mixer.cfg['cam']['whep'] + '/' + sid, method='DELETE'),
                               timeout=3).close()
    except Exception:
        pass
    return jsonify(ok=True)


@bp.route('/api/listenpos')
def api_listenpos():
    # Polled ~1/s by a playing page to measure how far behind live it is.
    return jsonify(_mixer.listener.position(request.args.get('id', '')))


def init_mixer(app):
    global _mixer
    _mixer = Mixer(load_config())
    _mixer.select_feed('main1')
    app.register_blueprint(bp)
    _mixer.start()
    print(f"[mixer] {_mixer.caps['model']} at {_mixer.ip or '(searching)'}; remote "
          f"{'ENABLED' if _mixer.cfg.get('remote_enabled') else 'disabled'}", flush=True)
    # A fader drag is ~20 POSTs/s: keep the successful ones (and the meter/event plumbing) out of
    # the journal. Anything that was NOT 2xx still gets logged, so rejected control is visible.
    noisy = ('/mixer/api/set', '/mixer/api/events', '/mixer/api/feed', '/mixer/api/node', '/mixer/api/mute',
             '/mixer/api/cam/set', '/mixer/api/listen/set',
             '/mixer/api/listenpos', '/mixer/api/sp/tracks', '/mixer/api/sp/playlists', '/mixer/api/sp/search')
    ansi, status = re.compile(r'\x1b\[[0-9;]*m'), re.compile(r'HTTP/[\d.]+" (\d{3}) ')

    def _quiet(r):
        msg = ansi.sub('', r.getMessage())
        if not any(p in msg for p in noisy):
            return True
        m = status.search(msg)
        return not (m and m.group(1).startswith('2'))

    logging.getLogger('werkzeug').addFilter(_quiet)
    return _mixer
