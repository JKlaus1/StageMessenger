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

from flask import Blueprint, Response, jsonify, request, send_from_directory, abort

from .wing import Wing, N_BUS, N_MTX, N_CH, N_AUX, SRC_GROUPS, SRC_LEAVES
from .listen import Listener
from .meters import Meters

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(HERE), 'mixer_config.json')

DEFAULTS = {
    'mixer_ip':        '192.168.0.91',
    'remote_enabled':  False,
    'usb_patch':       True,     # the Pi owns WING USB outs 1-43 and 47-48
    'ambient': {                 # USB 43: room/stage ambient mic
        'follow_channel': 10,    # use this channel's input source if the WING reports it
        'grp': 'B', 'in': 4,     # fallback when it doesn't
    },
    'bitrate':         '128k',
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
    r'|io/in/(?:LCL|A|B|C|SC|USB|CRD|MOD|PLAY|AES)/\d{1,2}/(?:g|vph|pol))$')
SRC_COUNT = dict(SRC_GROUPS)


class Mixer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hub = Hub()
        self.feed_id = 'main1'
        self.ambient_label = 'Ambient mic'
        self.patch_note = ''
        self._last_patch = 0.0
        self._patch_lock = threading.Lock()
        self.wing = Wing(cfg['mixer_ip'], on_update=self._on_update,
                         on_conn=self._on_conn, on_loaded=self._on_loaded)
        self.listener = Listener(bitrate=cfg.get('bitrate', '128k'),
                                 on_status=lambda st: self.hub.publish({'t': 'listen', 's': st}))

        self.meters = Meters(cfg['mixer_ip'], wanted=lambda: bool(self.hub.subs))

    def start(self):
        self.wing.start()
        self.meters.start()
        threading.Thread(target=self._meter_pump, daemon=True, name='meter-pump').start()
        threading.Thread(target=self._routing_poll, daemon=True, name='routing-poll').start()

    def _strips(self):
        return [('ch', i) for i in range(1, N_CH + 1)] + [('aux', i) for i in range(1, N_AUX + 1)]

    def _routing_poll(self):
        """The WING pushes nothing when a channel is re-patched, so re-read every strip's input
        patch and the physical-input settings (gain/48V/name) behind it every few seconds."""
        while True:
            time.sleep(3)
            if not self.wing.loaded:
                continue
            self.wing.poke([f'/{k}/{n}/in/conn/{leaf}' for k, n in self._strips() for leaf in ('grp', 'in')])
            time.sleep(0.2)
            self.wing.poke(self._source_addrs())

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
                              'c': lv['ch'], 'a': lv['aux'],
                              'b': [o for _, o in lv['bus']], 'm': [o for _, o in lv['main']]})

    # ── WING callbacks ──
    def _on_update(self, addr, v):
        self.hub.publish({'t': 'upd', 'a': addr, 'v': v})
        if not self.wing.loaded:
            return                                   # initial load: one snapshot at the end
        follow = (self.cfg.get('ambient') or {}).get('follow_channel')
        if addr.startswith('/io/out/USB/') or (follow and addr.startswith(f'/ch/{follow}/in/conn/')):
            threading.Thread(target=self.ensure_patch, daemon=True).start()
        if addr.endswith('name'):
            self.hub.publish({'t': 'feeds', 'feeds': self.feeds()})

    def _on_conn(self, ok):
        self.hub.publish({'t': 'conn', 'ok': ok})

    def _on_loaded(self):
        self.wing.query_many(self._source_addrs(), timeout=1.5)     # physical-input settings
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
    _mixer.hub.publish({'t': 'upd', 'a': addr, 'v': v})
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


@bp.route('/api/repatch', methods=['POST'])
def api_repatch():
    threading.Thread(target=_mixer.ensure_patch, kwargs={'force': True}, daemon=True).start()
    return jsonify(ok=True)


@bp.route('/stream.mp3')
def stream():
    threading.Thread(target=_mixer.ensure_patch, daemon=True).start()
    q = _mixer.listener.add_client()

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


def init_mixer(app):
    global _mixer
    _mixer = Mixer(load_config())
    _mixer.select_feed('main1')
    app.register_blueprint(bp)
    _mixer.start()
    print(f"[mixer] WING at {_mixer.cfg['mixer_ip']}; remote "
          f"{'ENABLED' if _mixer.cfg.get('remote_enabled') else 'disabled'}", flush=True)
    # A fader drag is ~20 POSTs/s: keep those (and the meter/event plumbing) out of the journal.
    logging.getLogger('werkzeug').addFilter(
        lambda r: not any(p in r.getMessage() for p in ('/mixer/api/set', '/mixer/api/events', '/mixer/api/feed')))
    return _mixer
