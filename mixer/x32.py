"""
Behringer X32 / Midas M32 OSC driver (UDP 10023) -- same interface as wing.Wing.

The page and controller speak the WING address dialect ("canonical": /ch/3/fdr in dB, /ch/3/$mute
0/1/2, /ch/3/tags '#M1,#M2', /mgrp/2/mute ...). This driver keeps the console's raw values and derives
the canonical ones, and translates canonical writes back. Canonical addresses use plain numbers
(/ch/3); raw X32 ones are zero-padded (/ch/03/mix/fader).

Protocol facts verified on an M32C fw 4.06-8 with an X-LIVE card (Oct 2026 probes, ~/x32_probe*.txt):
  * Query = address with no args; the reply carries the value. '/xremote' subscribes this socket to
    change pushes for 10 s -- renewed every 8 s. Pushes are plain OSC with the new value.
  * Faders (mix/fader, send mix/BB/level, dca fader): 0..1 with the 4-segment law
        f>=.5: 40f-30   f>=.25: 80f-50   f>=.0625: 160f-70   else 480f-90 dB (0 = -inf)
    -- identical to the WING's. 1024 steps; matched the console's own dB text on 50 faders.
  * Mute = mix/on, INVERTED (1 = unmuted). Mute groups: /config/mute/1..6 (no names), membership
    /ch/NN/grp/mute bitmask (bit 0 = group 1). Engaging a group really sets every member's mix/on 0
    (pushed); releasing sets every member's mix/on 1 -- including strips that were muted on their own
    before (console behaviour, confirmed). Override = write mix/on 1 while the group is engaged (works;
    the group stays lit; the next engage mutes it again). Buses can be group members too.
  * Solo: /-stat/solosw/01..80 (ch 1-32 = 01-32, aux in 33-40, fx rtn 41-48, bus 49-64, mtx 65-70,
    LR 71, M/C 72, dca 73-80); /-stat/solo = any solo; /-action/clearsolo 1 clears all.
  * Names /config/name (string), colour /config/color 0..15 (OFF RD GN YE BL MG CY WH, +8 = inverted).
  * Channel source /ch/NN/config/source: 0 OFF, 1-32 In01-32 (through the 8-channel input blocks
    /config/routing/IN/1-8.. or, while routswitch = 1 (PLAY), /config/routing/PLAY/..), 33-38 Aux 1-6,
    39/40 USB L/R, 41-48 FX 1L..4R, 49-64 Bus 1-16. Block values: 0-3 AN (local), 4-9 AES50 A,
    10-15 AES50 B, 16-19 Card, 20-23 user-in (/config/userrout/in/NN: 1-32 local, 33-80 A, 81-128 B,
    129-160 card, 161-166 aux in, 167/168 talkback). Re-patching IS pushed (unlike the WING).
  * /config/routing/routswitch 0 REC / 1 PLAY = the WING's MAIN/ALT. With the X-LIVE on AUTO the card
    flips it itself on PLAY / STOP.
  * Meters: separate subscription (see x32meters.py). All channel and bus meters are PRE-fader.
  * Input stage (v3.1): /ch/NN/preamp/trim (dB+18)/36, invert, hpon, hpf log 20..400 Hz, hpslope;
    aux in has trim + invert only. Preamp gain lives on the headamp the channel is patched to:
    /-ha/NN/index (ch 1-32 = 00-31, aux 33-40 = 32-39; -1 = no preamp, e.g. no stagebox) ->
    /headamp/HHH/gain (dB+12)/72 and /phantom; HHH 000-031 local, 032-079 AES50 A, 080-127 AES50 B.
    /-ha indexes are pushed when a stagebox comes or goes (and when PLAY routing flips).
  * Processing (v3.1), all 0..1 floats unless noted -- every scaling checked against the console's
    own text in the probes: EQ band f log 20..20k, g lin +-15, q = 10 * 0.03^n (10 .. 0.3), type int
    (LCut LShv PEQ VEQ HShv HCut). Gate thr lin -80..0, range 3..60, attack 0..120 ms, hold log
    0.02..2000 ms, release log 5..4000 ms, mode int (EXP2 EXP3 EXP4 GATE DUCK). Dyn thr lin -60..0,
    ratio int (1.1 .. 100 list), knee 0..5, mgain 0..24, attack/hold/release as the gate, mix 0..100,
    mode/det/env/pos ints. Key filter: filter/on, filter/type int (LC6 LC12 HC6 HC12 1.0 2.0 3.0 5.0
    10.0), filter/f log 20..20k. Aux in strips have EQ only (no gate / dyn / low cut).
"""
import socket
import struct
import threading
import time

from .wing import osc_msg

PORT = 10023
NEG_INF = -144.0

N_CH, N_AUX, N_BUS, N_MTX, N_MAIN, N_MGRP = 32, 8, 16, 6, 2, 6
BLOCKS = ('1-8', '9-16', '17-24', '25-32')
# X32 colour -> WING palette index used by the page (1..18; 0 = none)
COLOR_MAP = {0: 0, 1: 9, 2: 5, 3: 7, 4: 2, 5: 11, 6: 4, 7: 18}
KINDS = {'ch': N_CH, 'aux': N_AUX, 'bus': N_BUS, 'mtx': N_MTX, 'main': N_MAIN}
GAIN_RANGE, LCF_RANGE = (-12.0, 60.0), (20.0, 400.0)
SRC_LABEL = {'LCL': 'Local', 'A': 'AES A', 'B': 'AES B', 'CRD': 'Card', 'AUX': 'Aux', 'USB': 'USB', 'FX': 'FX',
             'BUS': 'Bus', 'TB': 'Talkback'}
# source picker groups -> channel 'config/source' value = base + index
PICK_GROUPS = [('IN', 32), ('AUX', 6), ('USB', 2), ('FX', 8), ('BUS', 16)]
PICK_BASE = {'IN': 0, 'AUX': 32, 'USB': 38, 'FX': 40, 'BUS': 48}

EQ_TYPES = ['LCut', 'LShv', 'PEQ', 'VEQ', 'HShv', 'HCut']
GATE_MODES = ['EXP2', 'EXP3', 'EXP4', 'GATE', 'DUCK']
DYN_RATIOS = ['1.1', '1.3', '1.5', '2.0', '2.5', '3.0', '4.0', '5.0', '7.0', '10', '20', '100']
KEY_FILTERS = ['LC6', 'LC12', 'HC6', 'HC12', '1.0', '2.0', '3.0', '5.0', '10.0']


def _p(key, raw, conv, lo=None, hi=None, unit='', steps=None, opts=None):
    """One page parameter: page 'type' is lin / log / int / list; 'conv' is how the raw 0..1 maps."""
    typ = {'qlog': 'log'}.get(conv, conv)
    return {'key': key, 'raw': raw, 'conv': conv, 'type': typ, 'lo': lo, 'hi': hi, 'unit': unit,
            'steps': steps, 'opts': opts}


def _key_filter(prefix):
    return [_p('fon', f'{prefix}/filter/on', 'int', 0, 1), _p('ftype', f'{prefix}/filter/type', 'list', opts=KEY_FILTERS),
            _p('ff', f'{prefix}/filter/f', 'log', 20, 20000, 'Hz', 201)]


NODE_SPECS = {
    'eq': [_p('on', 'eq/on', 'int', 0, 1)] + [x for b in range(1, 5) for x in (
        _p(f'{b}type', f'eq/{b}/type', 'list', opts=EQ_TYPES),
        _p(f'{b}g', f'eq/{b}/g', 'lin', -15, 15, 'dB', 121),
        _p(f'{b}f', f'eq/{b}/f', 'log', 20, 20000, 'Hz', 201),
        _p(f'{b}q', f'eq/{b}/q', 'qlog', 0.3, 10, '', 72))],
    'gate': [_p('on', 'gate/on', 'int', 0, 1), _p('mode', 'gate/mode', 'list', opts=GATE_MODES),
             _p('thr', 'gate/thr', 'lin', -80, 0, 'dB', 161), _p('range', 'gate/range', 'lin', 3, 60, 'dB', 58),
             _p('att', 'gate/attack', 'lin', 0, 120, 'ms', 121), _p('hld', 'gate/hold', 'log', 0.02, 2000, 'ms', 101),
             _p('rel', 'gate/release', 'log', 5, 4000, 'ms', 101)] + _key_filter('gate'),
    'dyn': [_p('on', 'dyn/on', 'int', 0, 1), _p('mode', 'dyn/mode', 'list', opts=['COMP', 'EXP']),
            _p('det', 'dyn/det', 'list', opts=['PEAK', 'RMS']), _p('env', 'dyn/env', 'list', opts=['LIN', 'LOG']),
            _p('thr', 'dyn/thr', 'lin', -60, 0, 'dB', 121), _p('ratio', 'dyn/ratio', 'list', opts=DYN_RATIOS),
            _p('knee', 'dyn/knee', 'lin', 0, 5, 'dB', 6), _p('gain', 'dyn/mgain', 'lin', 0, 24, 'dB', 49),
            _p('att', 'dyn/attack', 'lin', 0, 120, 'ms', 121), _p('hld', 'dyn/hold', 'log', 0.02, 2000, 'ms', 101),
            _p('rel', 'dyn/release', 'log', 5, 4000, 'ms', 101), _p('pos', 'dyn/pos', 'list', opts=['PRE', 'POST']),
            _p('mix', 'dyn/mix', 'lin', 0, 100, '%', 21), _p('auto', 'dyn/auto', 'int', 0, 1)] + _key_filter('dyn'),
}
NODE_ON = {'ch': ('eq', 'gate', 'dyn'), 'aux': ('eq',)}


def spec_value(sp, n):
    """raw -> display value (rounded the way the console shows it)"""
    c = sp['conv']
    if c in ('int', 'list'):
        return int(n) if isinstance(n, (int, float)) else None
    if not isinstance(n, (int, float)):
        return None
    n = max(0.0, min(1.0, float(n)))
    lo, hi = sp['lo'], sp['hi']
    if c == 'lin':
        v = lo + n * (hi - lo)
        if sp['steps'] and sp['steps'] > 1:
            st = (hi - lo) / (sp['steps'] - 1)
            v = lo + round((v - lo) / st) * st
        return round(v, 3)
    v = lo * (hi / lo) ** n if c == 'log' else 10 * 0.03 ** n
    return float(f'{v:.3g}')


def spec_norm(sp, v):
    """display value -> raw 0..1 (clamped to the console's range)"""
    import math
    lo, hi = sp['lo'], sp['hi']
    v = max(min(lo, hi), min(max(lo, hi), float(v)))
    if sp['conv'] == 'lin':
        n = (v - lo) / (hi - lo)
    elif sp['conv'] == 'log':
        n = math.log(v / lo) / math.log(hi / lo)
    else:                                            # qlog: Q 10 .. 0.3
        n = math.log(v / 10) / math.log(0.03)
    return max(0.0, min(1.0, n))


def ha_source(h):
    """headamp index -> (group, 1-based input)"""
    if not isinstance(h, int) or h < 0:
        return None, None
    if h < 32:
        return 'LCL', h + 1
    if h < 80:
        return 'A', h - 31
    if h < 128:
        return 'B', h - 79
    return None, None


def ha_index(g, i):
    base, cnt = {'LCL': (0, 32), 'A': (32, 48), 'B': (80, 48)}.get(g, (None, 0))
    return base + i - 1 if base is not None and 1 <= i <= cnt else None


def ha_slot(kind, n):
    """strip -> /-ha/NN slot (ch 1-32 -> 00-31, aux 1-8 -> 32-39)"""
    return (n - 1) if kind == 'ch' else 31 + n


def raw_base(kind, n):
    """canonical (kind, n) -> raw strip prefix"""
    if kind == 'main':
        return '/main/st' if n == 1 else '/main/m'
    pre = {'ch': '/ch', 'aux': '/auxin', 'bus': '/bus', 'mtx': '/mtx'}[kind]
    return f'{pre}/{n:02d}'


def solo_index(kind, n):
    return {'ch': 0, 'aux': 32, 'bus': 48, 'mtx': 64}.get(kind, 70) + n


def fader_db(f):
    if not isinstance(f, (int, float)) or f <= 0:
        return NEG_INF
    if f >= 0.5:
        d = f * 40 - 30
    elif f >= 0.25:
        d = f * 80 - 50
    elif f >= 0.0625:
        d = f * 160 - 70
    else:
        d = f * 480 - 90
    return round(d, 1)


def fader_norm(d):
    d = float(d)
    if d <= -90:
        return 0.0
    if d >= -10:
        p = (d + 30) / 40
    elif d >= -30:
        p = (d + 50) / 80
    elif d >= -60:
        p = (d + 70) / 160
    else:
        p = (d + 90) / 480
    return max(0.0, min(1.0, p))


def _parse(b):
    """OSC -> (addr, args); like wing.osc_parse but also reads blobs (meters)."""
    def rd(i):
        j = b.index(b'\0', i)
        return b[i:j].decode(errors='replace'), (j // 4 + 1) * 4
    addr, i = rd(0)
    if i >= len(b):
        return addr, []
    tags, i = rd(i)
    out = []
    for t in tags[1:]:
        if t == 's':
            v, i = rd(i)
        elif t == 'i':
            v = struct.unpack('>i', b[i:i + 4])[0]; i += 4
        elif t == 'f':
            v = struct.unpack('>f', b[i:i + 4])[0]; i += 4
        elif t == 'b':
            n = struct.unpack('>i', b[i:i + 4])[0]; i += 4
            v = b[i:i + n]; i += (n + 3) // 4 * 4
        else:
            break
        out.append(v)
    return addr, out


def decode_block(v, offset, user_in):
    """Input-block value + channel offset in the block -> (group code, 1-based index)."""
    if not isinstance(v, int):
        return None, None
    if 0 <= v <= 3:
        return 'LCL', 8 * v + offset + 1
    if 4 <= v <= 9:
        return 'A', 8 * (v - 4) + offset + 1
    if 10 <= v <= 15:
        return 'B', 8 * (v - 10) + offset + 1
    if 16 <= v <= 19:
        return 'CRD', 8 * (v - 16) + offset + 1
    if 20 <= v <= 23:
        return decode_user_in(user_in(8 * (v - 20) + offset + 1))
    return None, None


def decode_user_in(u):
    if not isinstance(u, int) or u <= 0:
        return 'OFF', None
    if u <= 32:
        return 'LCL', u
    if u <= 80:
        return 'A', u - 32
    if u <= 128:
        return 'B', u - 80
    if u <= 160:
        return 'CRD', u - 128
    if u <= 166:
        return 'AUX', u - 160
    return 'TB', u - 166


class X32:
    """Owns one UDP socket: raw cache, canonical derivation, writes, /xremote."""

    def __init__(self, ip, on_update=None, on_conn=None, on_loaded=None):
        self.ip = ip
        self.on_update = on_update or (lambda a, v: None)
        self.on_conn = on_conn or (lambda ok: None)
        self.on_loaded = on_loaded or (lambda: None)
        self.raw = {}                  # raw X32 addr -> value
        self.state = {}                # canonical addr -> value (what the page sees)
        self.info = {}                 # /xinfo: ip, name, model, fw
        self.lock = threading.RLock()
        self.connected = False
        self.loaded = False
        self.last_rx = 0.0
        self._waiters = {}             # raw addr -> Event
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(('', 0))
        self.sock.settimeout(0.5)
        self._build_maps()

    # ── address maps ──
    def _build_maps(self):
        """raw addr -> list of derive callbacks (each returns [(canonical addr, value)])."""
        self._derivers = {}
        add = lambda raw, fn: self._derivers.setdefault(raw, []).append(fn)
        for kind, cnt in KINDS.items():
            for n in range(1, cnt + 1):
                rb, cb = raw_base(kind, n), f'/{kind}/{n}'
                add(f'{rb}/config/name', lambda r=rb, c=cb: self._d_name(r, c))
                add(f'{rb}/config/color', lambda r=rb, c=cb: self._d_color(r, c))
                add(f'{rb}/mix/fader', lambda r=rb, c=cb: [(c + '/fdr', fader_db(self.raw.get(r + '/mix/fader')))])
                add(f'{rb}/mix/on', lambda k=kind, n=n: self._d_mute(k, n))
                if kind in ('ch', 'aux', 'bus'):
                    add(f'{rb}/grp/mute', lambda k=kind, n=n: self._d_mute(k, n))
                if kind in ('ch', 'aux'):
                    add(f'/-stat/solosw/{solo_index(kind, n):02d}',
                        lambda k=kind, n=n, c=cb: [(c + '/$solo', 1 if self.raw.get(
                            f'/-stat/solosw/{solo_index(k, n):02d}') else 0)])
                    add(f'{rb}/config/source', lambda k=kind, n=n: self._d_source(k, n))
                    for b in range(1, N_BUS + 1):
                        add(f'{rb}/mix/{b:02d}/level', lambda r=rb, c=cb, b=b: [
                            (f'{c}/send/{b}/lvl', fader_db(self.raw.get(f'{r}/mix/{b:02d}/level')))])
                        add(f'{rb}/mix/{b:02d}/on', lambda r=rb, c=cb, b=b: [
                            (f'{c}/send/{b}/on', 1 if self.raw.get(f'{r}/mix/{b:02d}/on') else 0)])
        for kind in ('ch', 'aux'):
            for n in range(1, KINDS[kind] + 1):
                rb, cb = raw_base(kind, n), f'/{kind}/{n}'
                add(f'{rb}/preamp/trim', lambda r=rb, c=cb: [(c + '/in/set/trim', self._trim(r))])
                add(f'{rb}/preamp/invert', lambda r=rb, c=cb: [(c + '/in/set/inv', 1 if self.raw.get(r + '/preamp/invert') else 0)])
                if kind == 'ch':
                    add(f'{rb}/preamp/hpon', lambda r=rb, c=cb: [(c + '/flt/lc', 1 if self.raw.get(r + '/preamp/hpon') else 0)])
                    add(f'{rb}/preamp/hpf', lambda r=rb, c=cb: [(c + '/flt/lcf', self._lcf(r))])
                add(f'/-ha/{ha_slot(kind, n):02d}/index', self._d_headamps)
        for h in range(128):
            add(f'/headamp/{h:03d}/gain', self._d_headamps)
            add(f'/headamp/{h:03d}/phantom', self._d_headamps)
        for g in range(1, N_MGRP + 1):
            add(f'/config/mute/{g}', lambda g=g: self._d_group(g))
        # anything that changes where a channel's input comes from -> re-derive every source
        for blk in BLOCKS:
            add(f'/config/routing/IN/{blk}', self._d_all_sources)
            add(f'/config/routing/PLAY/{blk}', self._d_all_sources)
        for u in range(1, 33):
            add(f'/config/userrout/in/{u:02d}', self._d_all_sources)
        add('/config/routing/routswitch', lambda: [('/io/altsw', 1 if self.raw.get('/config/routing/routswitch') else 0)]
            + self._d_all_sources())

    def load_addrs(self):
        """Every raw address the page needs, sends last (the page is usable before they finish)."""
        a = ['/config/routing/routswitch'] + [f'/config/routing/{t}/{b}' for t in ('IN', 'PLAY') for b in BLOCKS] \
            + [f'/config/userrout/in/{u:02d}' for u in range(1, 33)] + [f'/config/mute/{g}' for g in range(1, N_MGRP + 1)]
        for kind, cnt in KINDS.items():
            for n in range(1, cnt + 1):
                rb = raw_base(kind, n)
                a += [f'{rb}/config/name', f'{rb}/config/color', f'{rb}/mix/fader', f'{rb}/mix/on']
                if kind in ('ch', 'aux', 'bus'):
                    a.append(f'{rb}/grp/mute')
                if kind in ('ch', 'aux'):
                    a += [f'{rb}/config/source', f'/-stat/solosw/{solo_index(kind, n):02d}',
                          f'{rb}/preamp/trim', f'{rb}/preamp/invert', f'/-ha/{ha_slot(kind, n):02d}/index']
                if kind == 'ch':
                    a += [f'{rb}/preamp/hpon', f'{rb}/preamp/hpf']
        a += [f'/headamp/{h:03d}/{k}' for h in range(128) for k in ('gain', 'phantom')]
        for kind in ('ch', 'aux'):
            for n in range(1, KINDS[kind] + 1):
                rb = raw_base(kind, n)
                for b in range(1, N_BUS + 1):
                    a += [f'{rb}/mix/{b:02d}/level', f'{rb}/mix/{b:02d}/on']
        return a

    # ── derivations (raw -> canonical) ──
    def _d_name(self, rb, cb):
        n = self.raw.get(rb + '/config/name')
        n = n.strip() if isinstance(n, str) else ''
        return [(cb + '/name', n), (cb + '/$name', n)]

    def _d_color(self, rb, cb):
        c = self.raw.get(rb + '/config/color')
        v = COLOR_MAP.get(c % 8, 0) if isinstance(c, int) else 0
        return [(cb + '/col', v), (cb + '/$col', v)]

    def _engaged(self):
        return {g for g in range(1, N_MGRP + 1) if self.raw.get(f'/config/mute/{g}')}

    def _d_mute(self, kind, n):
        """WING-style mute triple: 'mute' = own mute, '$mute' 0 off / 1 own / 2 muted by a group,
        'tags' = '#Mn' group membership. Muted-and-in-an-engaged-group counts as group-muted (the
        M32 can't tell an own mute apart while the group holds it)."""
        rb, cb = raw_base(kind, n), f'/{kind}/{n}'
        on = self.raw.get(rb + '/mix/on')
        if on is None:
            return []
        if kind not in ('ch', 'aux', 'bus'):
            return [(cb + '/mute', 0 if on else 1)]
        mask = self.raw.get(rb + '/grp/mute') or 0
        groups = [g for g in range(1, N_MGRP + 1) if mask & (1 << (g - 1))]
        held = bool(set(groups) & self._engaged())
        if on:
            own, st = 0, 0
        elif held:
            own, st = 0, 2
        else:
            own, st = 1, 1
        out = [(cb + '/mute', own if kind != 'bus' else (0 if on else 1)),
               (cb + '/tags', ','.join(f'#M{g}' for g in groups))]
        if kind != 'bus':
            out.append((cb + '/$mute', st))
        return out

    def _d_group(self, g):
        out = [(f'/mgrp/{g}/mute', 1 if self.raw.get(f'/config/mute/{g}') else 0), (f'/mgrp/{g}/name', '')]
        for kind in ('ch', 'aux', 'bus'):
            for n in range(1, KINDS[kind] + 1):
                out += self._d_mute(kind, n)
        return out

    def _source(self, kind, n, play):
        s = self.raw.get(raw_base(kind, n) + '/config/source')
        if not isinstance(s, int):
            return None, None
        if s == 0:
            return 'OFF', None
        if 1 <= s <= 32:
            blk = BLOCKS[(s - 1) // 8]
            v = self.raw.get(f'/config/routing/{"PLAY" if play else "IN"}/{blk}')
            return decode_block(v, (s - 1) % 8, lambda u: self.raw.get(f'/config/userrout/in/{u:02d}'))
        if s <= 38:
            return 'AUX', s - 32
        if s <= 40:
            return 'USB', s - 38
        if s <= 48:
            return 'FX', s - 40
        if s <= 64:
            return 'BUS', s - 48
        return None, None

    def _trim(self, rb):
        t = self.raw.get(rb + '/preamp/trim')
        return round(t * 36 - 18, 2) if isinstance(t, (int, float)) else None

    def _lcf(self, rb):
        f = self.raw.get(rb + '/preamp/hpf')
        return round(LCF_RANGE[0] * (LCF_RANGE[1] / LCF_RANGE[0]) ** f) if isinstance(f, (int, float)) else None

    def _d_headamps(self):
        """Canonical /io/in/<grp>/<n>/g|vph for every headamp a channel is patched to RIGHT NOW (the
        console reports -1 when there is no preamp, e.g. no stagebox) -- None for the rest, so the
        page only offers gain / 48V that really exist."""
        live = set()
        for kind in ('ch', 'aux'):
            for n in range(1, KINDS[kind] + 1):
                h = self.raw.get(f'/-ha/{ha_slot(kind, n):02d}/index')
                if isinstance(h, int) and 0 <= h < 128:
                    live.add(h)
        out = []
        for h in range(128):
            g, i = ha_source(h)
            gain, ph = self.raw.get(f'/headamp/{h:03d}/gain'), self.raw.get(f'/headamp/{h:03d}/phantom')
            on = h in live and isinstance(gain, (int, float))
            out += [(f'/io/in/{g}/{i}/g', round(gain * 72 - 12, 1) if on else None),
                    (f'/io/in/{g}/{i}/vph', (1 if ph else 0) if on else None)]
        return out

    def _pick(self, kind, n):
        s = self.raw.get(raw_base(kind, n) + '/config/source')
        if not isinstance(s, int) or s <= 0:
            return 'OFF:0'
        for g, cnt in reversed(PICK_GROUPS):
            if s > PICK_BASE[g]:
                return f'{g}:{s - PICK_BASE[g]}' if s - PICK_BASE[g] <= cnt else 'OFF:0'
        return 'OFF:0'

    def _d_source(self, kind, n):
        cb = f'/{kind}/{n}'
        g, i = self._source(kind, n, False)
        ag, ai = self._source(kind, n, True)
        if g is None:
            return []
        return [(cb + '/in/conn/pick', self._pick(kind, n)),
                (cb + '/in/conn/grp', g), (cb + '/in/conn/in', i if i is not None else 0),
                (cb + '/in/conn/altgrp', ag or 'OFF'), (cb + '/in/conn/altin', ai if ai is not None else 0),
                (cb + '/in/set/altsrc', 1 if self.raw.get('/config/routing/routswitch') else 0)]

    def _d_all_sources(self):
        out = []
        for kind in ('ch', 'aux'):
            for n in range(1, KINDS[kind] + 1):
                out += self._d_source(kind, n)
        return out

    def _apply(self, raw_addr, value, notify=True):
        """Store a raw value and push every canonical value that changed because of it."""
        with self.lock:
            self.raw[raw_addr] = value
            ups = []
            for fn in self._derivers.get(raw_addr, ()):
                ups += fn()
            changed = []
            for a, v in ups:
                if self.state.get(a) != v:
                    self.state[a] = v
                    changed.append((a, v))
            ev = self._waiters.pop(raw_addr, None)
        if ev:
            ev.set()
        if notify:
            for a, v in changed:
                self.on_update(a, v)
        return changed

    # ── lifecycle ──
    def start(self):
        threading.Thread(target=self._rx_loop, daemon=True, name='x32-rx').start()
        threading.Thread(target=self._keepalive, daemon=True, name='x32-ka').start()

    def stop(self):
        self._stop.set()

    def _send(self, addr, *args):
        try:
            self.sock.sendto(osc_msg(addr, *args), (self.ip, PORT))
        except OSError:
            pass

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5); continue
            try:
                addr, args = _parse(data)
            except Exception:
                continue
            self.last_rx = time.time()
            if not self.connected:
                self.connected = True
                self.on_conn(True)
                threading.Thread(target=self._load_all, daemon=True, name='x32-load').start()
            if addr == '/xinfo' and len(args) >= 4:
                self.info = {'ip': args[0], 'name': args[1], 'model': args[2], 'fw': args[3]}
                continue
            if not args or addr.startswith(('/meters', '/-action/', 'node', '/node', '/status', '/info')):
                continue
            self._apply(addr, args[0], notify=self.loaded)

    def _keepalive(self):
        while not self._stop.is_set():
            self._send('/xremote')
            self._send('/xinfo')              # liveness probe (pushes only flow on change)
            if self.connected and time.time() - self.last_rx > 20:
                self.connected = False
                self.loaded = False
                self.on_conn(False)
            self._stop.wait(8)

    def _load_all(self):
        addrs = self.load_addrs()
        for attempt in range(2):                     # one retry pass for anything UDP dropped
            for a in addrs:
                if not self.connected or self._stop.is_set():
                    return
                self._send(a)
                time.sleep(0.0015)
            time.sleep(0.6)
            with self.lock:
                addrs = [a for a in addrs if a not in self.raw]
            if not addrs:
                break
        if addrs:
            print(f'[mixer] x32: {len(addrs)} addresses did not answer (e.g. {addrs[:3]})', flush=True)
        self.loaded = True
        self.on_loaded()

    # ── canonical -> raw ──
    @staticmethod
    def _split(addr):
        """'/ch/3/send/5/lvl' -> ('ch', 3, 'send/5/lvl'); None if not a strip address."""
        p = addr.strip('/').split('/', 2)
        if len(p) < 3 or p[0] not in KINDS:
            return None
        try:
            n = int(p[1])
        except ValueError:
            return None
        if not 1 <= n <= KINDS[p[0]]:
            return None
        return p[0], n, p[2]

    def raw_for(self, addr):
        """Raw addresses a canonical address is derived from (for query / poke)."""
        if addr.startswith('/mgrp/'):
            g = addr.split('/')[2]
            return [f'/config/mute/{g}'] if g.isdigit() and 1 <= int(g) <= N_MGRP else []
        if addr == '/io/altsw':
            return ['/config/routing/routswitch']
        if addr.startswith('/io/in/'):
            h = self._ha_of(addr)
            return [f'/headamp/{h:03d}/' + ('gain' if addr.endswith('/g') else 'phantom')] if h is not None else []
        sp = self._split(addr)
        if not sp:
            return []
        kind, n, leaf = sp
        rb = raw_base(kind, n)
        if leaf in ('name', '$name'):
            return [rb + '/config/name']
        if leaf in ('col', '$col'):
            return [rb + '/config/color']
        if leaf == 'fdr':
            return [rb + '/mix/fader']
        if leaf in ('mute', '$mute', 'tags'):
            return [rb + '/mix/on'] + ([rb + '/grp/mute'] if kind in ('ch', 'aux', 'bus') else []) \
                + [f'/config/mute/{g}' for g in range(1, N_MGRP + 1)]
        if leaf == '$solo' and kind in ('ch', 'aux'):
            return [f'/-stat/solosw/{solo_index(kind, n):02d}']
        if leaf == 'in/set/trim' and kind in ('ch', 'aux'):
            return [rb + '/preamp/trim']
        if leaf == 'in/set/inv' and kind in ('ch', 'aux'):
            return [rb + '/preamp/invert']
        if leaf in ('flt/lc', 'flt/lcf') and kind == 'ch':
            return [rb + ('/preamp/hpon' if leaf == 'flt/lc' else '/preamp/hpf')]
        if leaf.startswith('in/') and kind in ('ch', 'aux'):
            return [rb + '/config/source']
        if leaf.startswith('send/') and kind in ('ch', 'aux'):
            b = int(leaf.split('/')[1])
            return [f'{rb}/mix/{b:02d}/' + ('level' if leaf.endswith('lvl') else 'on')]
        return []

    def set(self, addr, value):
        """Canonical write -> raw write. Returns the canonical value written, or None if refused."""
        if addr.startswith('/mgrp/') and addr.endswith('/mute'):
            g = addr.split('/')[2]
            if not (g.isdigit() and 1 <= int(g) <= N_MGRP):
                return None
            v = 1 if int(value) else 0
            self._write(f'/config/mute/{g}', v)
            return v
        if addr == '/io/altsw':
            v = 1 if int(value) else 0
            self._write('/config/routing/routswitch', v)
            return v
        if addr.startswith('/io/in/'):
            h = self._ha_of(addr)
            if h is None or self.state.get(addr) is None:     # only headamps that are really there
                return None
            if addr.endswith('/g'):
                v = round(max(GAIN_RANGE[0], min(GAIN_RANGE[1], float(value))) * 2) / 2
                self._write(f'/headamp/{h:03d}/gain', float((v - GAIN_RANGE[0]) / (GAIN_RANGE[1] - GAIN_RANGE[0])))
                return v
            if addr.endswith('/vph'):
                v = 1 if int(value) else 0
                self._write(f'/headamp/{h:03d}/phantom', v)
                return v
            return None
        sp = self._split(addr)
        if not sp:
            return None
        kind, n, leaf = sp
        rb = raw_base(kind, n)
        if leaf == 'in/set/trim' and kind in ('ch', 'aux'):
            v = round(max(-18.0, min(18.0, float(value))) * 4) / 4
            self._write(rb + '/preamp/trim', float((v + 18) / 36))
            return v
        if leaf == 'in/set/inv' and kind in ('ch', 'aux'):
            v = 1 if int(value) else 0
            self._write(rb + '/preamp/invert', v)
            return v
        if leaf == 'flt/lc' and kind == 'ch':
            v = 1 if int(value) else 0
            self._write(rb + '/preamp/hpon', v)
            return v
        if leaf == 'flt/lcf' and kind == 'ch':
            import math
            f = max(LCF_RANGE[0], min(LCF_RANGE[1], float(value)))
            self._write(rb + '/preamp/hpf', float(math.log(f / LCF_RANGE[0]) / math.log(LCF_RANGE[1] / LCF_RANGE[0])))
            return self._lcf(rb)
        if leaf == 'fdr':
            p = fader_norm(max(NEG_INF, min(10.0, float(value))))
            self._write(rb + '/mix/fader', float(p))
            return fader_db(p)
        if leaf == 'mute':
            v = 1 if int(value) else 0
            self._write(rb + '/mix/on', 0 if v else 1)
            return v
        if leaf == '$solo' and kind in ('ch', 'aux'):
            v = 1 if int(value) else 0
            self._write(f'/-stat/solosw/{solo_index(kind, n):02d}', v)
            return v
        if leaf.startswith('send/') and kind in ('ch', 'aux'):
            parts = leaf.split('/')
            b = int(parts[1])
            if not 1 <= b <= N_BUS:
                return None
            if parts[2] == 'lvl':
                p = fader_norm(max(NEG_INF, min(10.0, float(value))))
                self._write(f'{rb}/mix/{b:02d}/level', float(p))
                return fader_db(p)
            if parts[2] == 'on':
                v = 1 if int(value) else 0
                self._write(f'{rb}/mix/{b:02d}/on', v)
                return v
        return None

    def _write(self, raw_addr, value):
        self._send(raw_addr, value)
        self._apply(raw_addr, value, notify=self.loaded)      # the console doesn't echo our own writes

    @staticmethod
    def _ha_of(addr):
        p = addr.split('/')                          # ['', 'io', 'in', grp, n, leaf]
        if len(p) != 6 or p[5] not in ('g', 'vph') or not p[4].isdigit():
            return None
        return ha_index(p[3], int(p[4]))

    def _query_raw(self, addrs, timeout=0.5):
        evs = []
        with self.lock:
            for r in addrs:
                ev = threading.Event(); self._waiters[r] = ev; evs.append(ev)
        for r in addrs:
            self._send(r); time.sleep(0.0005)
        end = time.time() + timeout
        for ev in evs:
            ev.wait(max(0.0, end - time.time()))
        with self.lock:
            for r in addrs:
                self._waiters.pop(r, None)
            return {r: self.raw.get(r) for r in addrs}

    @staticmethod
    def _node_target(path):
        """'/ch/3/eq' -> ('ch', 3, 'eq') if the M32 has that block on that strip, else None."""
        p = (path or '').strip('/').split('/')
        if len(p) != 3 or p[0] not in NODE_ON or not p[1].isdigit() or p[2] not in NODE_ON[p[0]]:
            return None
        n = int(p[1])
        return (p[0], n, p[2]) if 1 <= n <= KINDS[p[0]] else None

    def node(self, path):
        """Processing block as the page's parameter list (same shape as wing.parse_describe), read
        fresh from the console each call (the page polls it while a tab is open)."""
        t = self._node_target(path)
        if not t:
            return []
        kind, n, blk = t
        rb = raw_base(kind, n)
        specs = NODE_SPECS[blk]
        got = self._query_raw([f'{rb}/{sp["raw"]}' for sp in specs], timeout=0.6)
        out = []
        for sp in specs:
            v = spec_value(sp, got.get(f'{rb}/{sp["raw"]}'))
            if v is None:
                continue
            p = {'key': sp['key'], 'type': sp['type'], 'ro': False}
            if sp['conv'] == 'list':
                if not 0 <= v < len(sp['opts']):
                    continue
                p.update(opts=list(sp['opts']), value=sp['opts'][v])
            else:
                p.update(lo=sp['lo'], hi=sp['hi'], unit=sp['unit'], steps=sp['steps'], value=v)
            out.append(p)
        return out

    def node_set(self, path, key, value):
        """-> (display value, None) or (None, error)"""
        t = self._node_target(path)
        if not t:
            return None, 'bad node'
        kind, n, blk = t
        sp = next((x for x in NODE_SPECS[blk] if x['key'] == key), None)
        if not sp:
            return None, 'parameter not writable'
        raw = f'{raw_base(kind, n)}/{sp["raw"]}'
        if sp['conv'] == 'list':
            v = str(value).strip()
            if v not in sp['opts']:
                return None, 'not an option'
            self._write(raw, sp['opts'].index(v))
            return v, None
        if sp['conv'] == 'int':
            v = int(max(sp['lo'], min(sp['hi'], round(float(value)))))
            self._write(raw, v)
            return v, None
        nv = spec_norm(sp, value)
        self._write(raw, float(nv))
        return spec_value(sp, nv), None

    def source_names(self, grp):
        cnt = dict(PICK_GROUPS).get(grp, 0)
        out = []
        for i in range(1, cnt + 1):
            if grp == 'IN':
                g, k = decode_block(self.raw.get(f'/config/routing/IN/{BLOCKS[(i - 1) // 8]}'), (i - 1) % 8,
                                    lambda u: self.raw.get(f'/config/userrout/in/{u:02d}'))
                name = f'{SRC_LABEL.get(g, g)} {k}' if g and g != 'OFF' else 'off'
            elif grp == 'BUS':
                name = self.state.get(f'/bus/{i}/name') or ''
            elif grp == 'FX':
                name = f'FX {(i + 1) // 2}{"LR"[(i - 1) % 2]}'
            elif grp == 'USB':
                name = 'USB ' + 'LR'[i - 1]
            else:
                name = ''
            out.append({'n': i, 'name': name, 'mode': ''})
        return out

    def patch_source(self, kind, n, grp, idx):
        """Re-patch a strip: picker group + index -> config/source. Read back to confirm."""
        if kind not in ('ch', 'aux') or not 1 <= n <= KINDS[kind] or grp not in PICK_BASE \
                or not 1 <= idx <= dict(PICK_GROUPS)[grp]:
            return False
        rb = raw_base(kind, n)
        want = PICK_BASE[grp] + idx
        self._write(rb + '/config/source', want)
        got = self._query_raw([rb + '/config/source'], timeout=0.6)
        return got.get(rb + '/config/source') == want

    def toggle_mute(self, kind, n):
        """Console MUTE-button semantics on the M32: muted (own or by a group) -> unmute (for a group
        member that's the override; the group stays engaged); unmuted -> mute."""
        rb, b = raw_base(kind, n), f'/{kind}/{n}'
        before = self.query(b + '/$mute')
        if before is None:
            return False, 'offline', None
        on = self.raw.get(rb + '/mix/on')
        self._write(rb + '/mix/on', 0 if on else 1)
        time.sleep(0.12)
        after = self.query(b + '/$mute')
        action = 'override' if before == 2 else 'own'
        return True, action, after

    def clear_solo(self):
        self._send('/-action/clearsolo', 1)

    # ── public api (same shape as wing.Wing) ──
    def query(self, addr, timeout=0.5):
        """Blocking read of a canonical address (re-reads the raw values behind it)."""
        raws = self.raw_for(addr)
        if not raws:
            return None
        evs = []
        with self.lock:
            for r in raws:
                ev = threading.Event(); self._waiters[r] = ev; evs.append(ev)
        for r in raws:
            self._send(r)
        end = time.time() + timeout
        ok = all(ev.wait(max(0.0, end - time.time())) for ev in evs)
        with self.lock:
            for r in raws:
                self._waiters.pop(r, None)
            return self.state.get(addr) if ok or addr in self.state else None

    def query_many(self, addrs, timeout=1.0):
        raws = sorted({r for a in addrs for r in self.raw_for(a)})
        evs = []
        with self.lock:
            for r in raws:
                ev = threading.Event(); self._waiters[r] = ev; evs.append(ev)
        for r in raws:
            self._send(r); time.sleep(0.001)
        end = time.time() + timeout
        for ev in evs:
            ev.wait(max(0.0, end - time.time()))
        with self.lock:
            for r in raws:
                self._waiters.pop(r, None)
            return {a: self.state.get(a) for a in addrs}

    def poke(self, addrs, pace=0.001):
        for r in sorted({r for a in addrs for r in self.raw_for(a)}):
            self._send(r); time.sleep(pace)

    def describe(self, node, timeout=1.0):
        return None                                  # no '#' node descriptions on the X32

    def get(self, addr, default=None):
        with self.lock:
            return self.state.get(addr, default)

    def get_raw(self, addr, default=None):
        with self.lock:
            return self.raw.get(addr, default)

    def snapshot(self):
        with self.lock:
            return dict(self.state)


def probe(ip, timeout=0.6):
    """-> /xinfo dict if an X32/M32 answers at ip, else None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(osc_msg('/xinfo'), (ip, PORT))
        end = time.time() + timeout
        while time.time() < end:
            d, _ = s.recvfrom(4096)
            addr, args = _parse(d)
            if addr == '/xinfo' and len(args) >= 4:
                return {'ip': args[0], 'name': args[1], 'model': args[2], 'fw': args[3]}
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    return None
