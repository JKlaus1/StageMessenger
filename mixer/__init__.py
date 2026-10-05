"""
WING remote mixer + listen-back for Stage Messenger.

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
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, Response, jsonify, request, send_from_directory, abort

from .wing import Wing, N_BUS, N_MTX, N_CH, N_AUX, N_SD, SRC_GROUPS, SRC_LEAVES, parse_describe
from .listen import Listener
from .meters import Meters

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(HERE), 'mixer_config.json')
STATE_PATH = os.path.join(os.path.dirname(HERE), 'mixer_state.json')

DEFAULTS = {
    'mixer_ip':        '192.168.0.91',
    'remote_enabled':  False,
    'usb_patch':       True,     # the Pi owns WING USB outs 1-43 and 47-48
    'ambient': {                 # USB 43: room/stage ambient mic
        'follow_channel': 10,    # use this channel's input source if the WING reports it
        'grp': 'B', 'in': 4,     # fallback when it doesn't
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
        'opus_bitrate':   '96k',
        'turn_key_id':    '',        # Cloudflare Realtime TURN key -- set these in mixer_config.json,
        'turn_api_token': '',        # never in git
        'turn_ttl':       86400,
    },
    # Pi playback (v2.4): go-librespot "Stage Rig" -> WING USB 1/2 -> AUX 1 (see mixer/spotify.py)
    'spotify': {
        'enabled':    True,
        'api':        'http://127.0.0.1:3678',
        'aux':        1,                              # the WING aux strip USB 1/2 feeds
        'config_dir': '/home/pi/.config/go-librespot',
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
# Assumption (verify by ear): stereo sources occupy consecutive 'in' indices,
# e.g. BUS in 1/2 = Bus 1 L/R, 3/4 = Bus 2 L/R.

def feed_table():
    """[(feed_id, usb_left_1based, usb_right_1based, grp, in_left, in_right)]"""
    t = [('main1', 1, 2, 'MAIN', 1, 2), ('mon1', 47, 48, 'MON', 1, 2)]
    for b in range(1, N_BUS + 1):
        u = 3 + 2 * (b - 1)
        t.append((f'bus{b}', u, u + 1, 'BUS', 2 * b - 1, 2 * b))
    for m in range(1, 5):
        u = 35 + 2 * (m - 1)
        t.append((f'mtx{m}', u, u + 1, 'MTX', 2 * m - 1, 2 * m))
    return t

AMBIENT_USB = 43

# WING input-source group codes -> labels shown in the UI
SRC_NAMES = {'LCL': 'Local', 'A': 'AES A', 'B': 'AES B', 'C': 'AES C', 'SC': 'StageCon',
             'USB': 'USB', 'CRD': 'Card', 'MOD': 'Module', 'PLAY': 'Player', 'AES': 'AES/EBU',
             'USR': 'User', 'OSC': 'Osc', 'AUX': 'Aux'}


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
    r'|mgrp/[1-8]/mute'
    r'|(?:ch|aux)/\d{1,2}/(?:in/set/(?:trim|inv)|flt/(?:lc|lcf|hc|hcf))'
    r'|io/in/(?:LCL|A|B|C|SC|USB|CRD|MOD|PLAY|AES)/\d{1,2}/(?:g|vph|pol)'
    r'|io/altsw|cards/wlive/auto_(?:play|rec|stop))$')
LOGGED_SETS = ('/io/altsw', '/cards/wlive/auto_')   # console-wide changes: note who made them
SRC_COUNT = dict(SRC_GROUPS)
NODE_PATH = re.compile(r'^/(ch|aux)/(\d{1,2})/(eq|gate|dyn)$')
NODE_LOCKED = ('mdl',)          # model changes stay at the console for now


def _node_ok(path):
    m = NODE_PATH.match(path or '')
    return bool(m) and 1 <= int(m.group(2)) <= {'ch': N_CH, 'aux': N_AUX}[m.group(1)]


class Mixer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hub = Hub()
        self.feed_id = 'main1'
        self.ambient_label = 'Ambient mic'
        self.node_cache = {}
        self._ovr_lock = threading.RLock()
        self._load_state()
        self.patch_note = ''
        self._last_patch = 0.0
        self._patch_lock = threading.Lock()
        self.wing = Wing(cfg['mixer_ip'], on_update=self._on_update,
                         on_conn=self._on_conn, on_loaded=self._on_loaded)
        rtc = cfg.get('rtc', {})
        self.rtc_enabled = bool(rtc.get('enabled'))
        self._turn, self._turn_exp, self._turn_lock = None, 0.0, threading.Lock()
        self.rec_markers = {n: [] for n in range(1, N_SD + 1)}   # card -> marker times (current session)
        self.rec_sessions = {n: [] for n in range(1, N_SD + 1)}  # card -> session list (newest first)
        self._rec_pub = {}                                        # throttle for 10 Hz etime / sdfree pushes
        self._rec_tail = set()                                    # throttled addrs awaiting a trailing publish
        self.listener = Listener(bitrate=cfg.get('bitrate', '128k'),
                                 rtc_url=rtc.get('rtsp', '') if self.rtc_enabled else '',
                                 opus_bitrate=rtc.get('opus_bitrate', '96k'), rtc_probe=self.rtc_probe,
                                 cushion_s=cfg.get('cushion_s', 0.5), max_queue_s=cfg.get('max_queue_s', 1.0),
                                 on_status=lambda st: self.hub.publish({'t': 'listen', 's': st}))

        self.meters = Meters(cfg['mixer_ip'], wanted=lambda: bool(self.hub.subs))
        self.spotify = None                           # Pi playback is optional: never blocks the mixer
        if (cfg.get('spotify') or {}).get('enabled'):
            try:
                from .spotify import Spotify
                self.spotify = Spotify(cfg['spotify'], self.hub.publish, lambda: bool(self.hub.subs))
            except Exception as e:
                print(f'[mixer] Pi playback disabled: {e}', flush=True)

    def start(self):
        self.wing.start()
        self.meters.start()
        threading.Thread(target=self._meter_pump, daemon=True, name='meter-pump').start()
        threading.Thread(target=self._routing_poll, daemon=True, name='routing-poll').start()
        if self.spotify:
            try:
                self.spotify.start()
            except Exception as e:
                print(f'[mixer] Pi playback disabled: {e}', flush=True)
                self.spotify = None

    def _strips(self):
        return [('ch', i) for i in range(1, N_CH + 1)] + [('aux', i) for i in range(1, N_AUX + 1)]

    def _routing_poll(self):
        """The WING pushes nothing when a channel is re-patched, so re-read every strip's input
        patch and the physical-input settings (gain/48V/name) behind it every few seconds."""
        while True:
            time.sleep(3)
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

    def _load_state(self):
        self.overrides, self.order = {}, []
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
            self.overrides = {k: list(v) for k, v in data.get('overrides', {}).items()}
            self.order = self.clean_order(data.get('order', []))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f'[mixer] bad {STATE_PATH}: {e}', flush=True)

    def _write_state(self):
        try:
            tmp = STATE_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'overrides': self.overrides, 'order': self.order}, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f'[mixer] could not save {STATE_PATH}: {e}', flush=True)

    def _save_state(self):
        self._write_state()
        self.hub.publish({'t': 'ovr', 'v': sorted(self.overrides)})

    # ── channel display order (page only; shared by every device, never sent to the WING) ──
    @staticmethod
    def clean_order(order):
        valid = {f'{k}/{n}' for k, n in [('ch', i) for i in range(1, N_CH + 1)] + [('aux', i) for i in range(1, N_AUX + 1)]}
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

    def _source_addrs(self, extra=()):
        srcs = set(extra)
        for k, n in self._strips():
            g, i = self.wing.get(f'/{k}/{n}/in/conn/grp'), self.wing.get(f'/{k}/{n}/in/conn/in')
            if g in SRC_COUNT and isinstance(i, int) and 1 <= i <= SRC_COUNT[g]:
                srcs.add((g, i))
        return [f'/io/in/{g}/{i}/{leaf}' for g, i in sorted(srcs) for leaf in SRC_LEAVES]

    def patch(self, kind, n, grp, idx):
        """Re-patch a channel/aux input. grp first, then in -- then read everything back."""
        base = f'/{kind}/{n}/in/conn'
        self.wing.set(base + '/grp', grp); time.sleep(0.03)
        self.wing.set(base + '/in', idx); time.sleep(0.05)
        got = self.wing.query_many([base + '/grp', base + '/in']
                                   + [f'/io/in/{grp}/{idx}/{leaf}' for leaf in SRC_LEAVES], timeout=1.0)
        for a, v in got.items():
            if v is not None:
                self.hub.publish({'t': 'upd', 'a': a, 'v': v})
        follow = (self.cfg.get('ambient') or {}).get('follow_channel')
        if kind == 'ch' and n == follow:
            threading.Thread(target=self.ensure_patch, kwargs={'force': True}, daemon=True).start()
        print(f'[mixer] patched {kind} {n} -> {grp} {idx}', flush=True)
        return got.get(base + '/grp') == grp and got.get(base + '/in') == idx

    # ── processing nodes (EQ / gate / dyn): driven by the console's own '#' description ──
    def node(self, path):
        params = parse_describe(self.wing.describe(path))
        if params:
            self.node_cache[path] = params
        return params

    def node_set(self, path, key, value):
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
                              'b': [r[1] for r in lv['bus']], 'm': [r[1] for r in lv['main']]})

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
        follow = (self.cfg.get('ambient') or {}).get('follow_channel')
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
        self.wing.query_many(self._source_addrs(), timeout=1.5)     # physical-input settings
        if self.overrides:                                          # after a restart / reconnect
            self.wing.query_many([b + '/tags' for b in self.overrides]
                                 + [f'/mgrp/{g}/mute' for g in range(1, 9)], timeout=1.0)
            self.check_overrides()
        for n in range(1, N_SD + 1):
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
        out = []
        for fid, ul, ur, grp, il, ir in feed_table():
            if fid == 'main1':
                label = f"Main {self._name('/main/1/name', 'LR')}"
            elif fid == 'mon1':
                label = 'Monitor 1 (phones / solo)'
            elif fid.startswith('bus'):
                b = fid[3:]; label = f"Bus {b} – {self._name(f'/bus/{b}/name', '')}".rstrip(' –')
            else:
                m = fid[3:]; label = f"Mtx {m} – {self._name(f'/mtx/{m}/name', '')}".rstrip(' –')
            out.append({'id': fid, 'label': label, 'usb': [ul, ur]})
        out.append({'id': 'ambient', 'label': self.ambient_label, 'usb': [AMBIENT_USB, AMBIENT_USB]})
        return out

    def select_feed(self, fid):
        for f in self.feeds():
            if f['id'] == fid:
                self.feed_id = fid
                self.listener.set_pair(f['usb'][0] - 1, f['usb'][1] - 1)
                self.hub.publish({'t': 'feed', 'id': fid})
                return True
        return False

    # ── USB patch ownership ──
    def _ambient_source(self):
        amb = self.cfg.get('ambient') or {}
        ch = amb.get('follow_channel')
        if ch:
            g = self.wing.query(f'/ch/{ch}/in/conn/grp')
            i = self.wing.query(f'/ch/{ch}/in/conn/in')
            if isinstance(g, str) and g and g != 'OFF' and isinstance(i, int) and i > 0:
                return g, i, f'follows Ch {ch}'
        return amb.get('grp', 'B'), int(amb.get('in', 4)), 'fixed'

    def _set_ambient_label(self, g, i):
        src = self.wing.query(f'/io/in/{g}/{i}/name')
        lab = f'Ambient \u00b7 {SRC_NAMES.get(g, g)} {i}'
        if isinstance(src, str) and src.strip():
            lab += f' ({src.strip()})'
        if lab != self.ambient_label:
            self.ambient_label = lab
            self.hub.publish({'t': 'feeds', 'feeds': self.feeds()})

    def ensure_patch(self, force=False):
        """Make WING USB outs match feed_table + ambient. Writes only what differs."""
        if not self.cfg.get('usb_patch', True) or not self.wing.connected:
            return
        with self._patch_lock:
            if not force and time.time() - self._last_patch < 10:   # never fight a recall loop
                return
            self._last_patch = time.time()
            want = []
            for _, ul, ur, grp, il, ir in feed_table():
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

    # ── snapshot ──
    # ── WING-LIVE SD recorder ──
    # The marker and session *lists* only exist as the option lists of $stat/markerlist and
    # $stat/sessionlist (a push carries just the selected entry), so they are read from the node
    # description whenever the count / state / open session changes.
    def refresh_markers(self, card, delay=0.0, publish=True):
        def go():
            if delay:
                time.sleep(delay)
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
            'state':  self.wing.snapshot(),
            'feeds':  self.feeds(),
            'feed':   self.feed_id,
            'listen': self.listener.status(),
            'patch':  self.patch_note,
            'nbus':   N_BUS,
            'meters': self.meters.levels is not None,
            'srcgroups': SRC_GROUPS,
            'ovr':    sorted(self.overrides),
            'order':  self.order,
            'listen_target': self.cfg.get('listen_target_s', 0.8),
            'recm':   self.rec_markers,
            'recs':   self.rec_sessions,
            'sp':     self.spotify.snapshot() if self.spotify else None,
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


@bp.route('', strict_slashes=False)
def page():
    resp = send_from_directory(HERE, 'mixer.html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/api/state')
def api_state():
    return jsonify(_mixer.snapshot())


@bp.route('/api/events')
def api_events():
    q = _mixer.hub.subscribe()

    def gen():
        try:
            yield 'retry: 2000\n\n'
            yield 'data: ' + json.dumps({'t': 'snap', **_mixer.snapshot()}, separators=(',', ':')) + '\n\n'
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
    d = request.get_json(silent=True) or {}
    kind, grp = str(d.get('kind', '')), str(d.get('grp', ''))
    try:
        n, idx = int(d.get('n')), int(d.get('in'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad number'), 400
    lim = {'ch': N_CH, 'aux': N_AUX}.get(kind)
    if not lim or not 1 <= n <= lim or grp not in SRC_COUNT or not 1 <= idx <= SRC_COUNT[grp]:
        return jsonify(ok=False, err='bad patch target'), 400
    return jsonify(ok=_mixer.patch(kind, n, grp, idx))


@bp.route('/api/srcnames')
def api_srcnames():
    grp = request.args.get('g', '')
    if grp not in SRC_COUNT:
        return jsonify(ok=False, err='bad group'), 400
    return jsonify(ok=True, g=grp, inputs=_mixer.source_names(grp))


@bp.route('/api/node')
def api_node():
    path = request.args.get('path', '')
    if not _node_ok(path):
        return jsonify(ok=False, err='bad node'), 400
    params = _mixer.node(path)
    return jsonify(ok=bool(params), path=path, params=params)


@bp.route('/api/nodeset', methods=['POST'])
def api_nodeset():
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
    lim = {'ch': N_CH, 'aux': N_AUX}.get(kind)
    if not lim or not 1 <= n <= lim:
        return jsonify(ok=False, err='bad strip'), 400
    ok, action, state = _mixer.toggle_mute(kind, n)
    return jsonify(ok=ok, action=action, state=state)


@bp.route('/api/order', methods=['POST'])
def api_order():
    d = request.get_json(silent=True) or {}
    if not isinstance(d.get('order'), list):
        return jsonify(ok=False, err='order must be a list'), 400
    return jsonify(ok=True, order=_mixer.set_order(d['order']))


@bp.route('/api/repatch', methods=['POST'])
def api_repatch():
    threading.Thread(target=_mixer.ensure_patch, kwargs={'force': True}, daemon=True).start()
    return jsonify(ok=True)


@bp.route('/stream.mp3')
def stream():
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
    d = request.get_json(silent=True) or {}
    action, card = str(d.get('action', '')), d.get('card')
    playback = False
    if card == 'all' and action == 'marker':
        cards = [n for n in range(1, N_SD + 1) if _mixer.rec_state(n) == 'REC']
        if not cards:
            cards = [n for n in range(1, N_SD + 1) if _mixer.rec_state(n) in ('PLAY', 'PPAUSE')
                     and _mixer.wing.get(f'/cards/wlive/{n}/$stat/sdstate') == 'READY']
            playback = True
        if not cards:
            return jsonify(ok=False, err='nothing is recording or playing'), 409
    else:
        try:
            cards = [int(card)]
        except (TypeError, ValueError):
            return jsonify(ok=False, err='bad card'), 400
        if not 1 <= cards[0] <= N_SD:
            return jsonify(ok=False, err='bad card'), 400
    if action not in ('rec', 'stop', 'marker'):
        return jsonify(ok=False, err='bad action'), 400
    if not _mixer.wing.connected:
        return jsonify(ok=False, err='WING offline'), 503
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
    d = request.get_json(silent=True) or {}
    action = str(d.get('action', ''))
    try:
        card = int(d.get('card'))
    except (TypeError, ValueError):
        return jsonify(ok=False, err='bad card'), 400
    if not 1 <= card <= N_SD:
        return jsonify(ok=False, err='bad card'), 400
    if action not in PLAY_ACTIONS:
        return jsonify(ok=False, err='bad action'), 400
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
        return jsonify(ok=False, err='WING offline'), 503
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


# ── Pi playback (v2.4): go-librespot "Stage Rig" -- see mixer/spotify.py ──
@bp.route('/api/sp/cmd', methods=['POST'])
def api_sp_cmd():
    """{cmd: playpause|next|prev|seek|shuffle|repeat|disconnect, v}. Forwarded to go-librespot."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Pi playback is not enabled'), 503
    d = request.get_json(silent=True) or {}
    cmd, v = str(d.get('cmd', '')), d.get('v')
    if cmd not in sp.CMDS:
        return jsonify(ok=False, err='bad command'), 400
    ok, err = sp.command(cmd, v)
    if cmd in ('disconnect', 'shuffle', 'repeat', 'next', 'prev', 'playpause') or not ok:
        print(f'[mixer] spotify: {cmd.upper()}{"" if v is None else " " + str(v)} from {_who()}'
              f'{"" if ok else " -- REFUSED: " + err}', flush=True)
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, err=err), 409)


@bp.route('/api/sp/service', methods=['POST'])
def api_sp_service():
    """Kill switch, service level: {action: restart|repair}. repair = sign out (forget the saved
    login) and restart -> a new pairing code shows on the page."""
    sp = _mixer.spotify
    if not sp:
        return jsonify(ok=False, err='Pi playback is not enabled'), 503
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
    if not _mixer.rtc_enabled:
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
    print(f"[mixer] WING at {_mixer.cfg['mixer_ip']}; remote "
          f"{'ENABLED' if _mixer.cfg.get('remote_enabled') else 'disabled'}", flush=True)
    # A fader drag is ~20 POSTs/s: keep the successful ones (and the meter/event plumbing) out of
    # the journal. Anything that was NOT 2xx still gets logged, so rejected control is visible.
    noisy = ('/mixer/api/set', '/mixer/api/events', '/mixer/api/feed', '/mixer/api/node', '/mixer/api/mute',
             '/mixer/api/listenpos')
    ansi, status = re.compile(r'\x1b\[[0-9;]*m'), re.compile(r'HTTP/[\d.]+" (\d{3}) ')

    def _quiet(r):
        msg = ansi.sub('', r.getMessage())
        if not any(p in msg for p in noisy):
            return True
        m = status.search(msg)
        return not (m and m.group(1).startswith('2'))

    logging.getLogger('werkzeug').addFilter(_quiet)
    return _mixer
