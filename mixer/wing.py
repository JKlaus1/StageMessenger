"""
Behringer WING OSC bridge (UDP 2223).

Protocol facts verified against a WING Rack, fw 3.1 (Oct 2026 probes):
  * Query = address with an empty type tag. Reply args: [display_str, norm_0_1, native].
    String params (names, USB grp) reply with a single string arg.
  * '/*S' subscribes this socket to change pushes (1 arg, native value). Renewed every 5 s.
    Pushes also arrive on '$'-prefixed shadow paths (/ch/1/$fdr) -- ignored, except
    '$name' = the displayed name (plain 'name' can be stale when the name follows the source).
  * Solo: /ch/N/$solo is writable (int 0/1); it lands on the monitor buses (MON group).
  * Color: col / $col (displayed) = palette 1..18 (display string; native is 0-based).
  * Mute groups: /mgrp/1-8/name|mute; a strip's membership is in its 'tags' ("#M1,#M2").
    /ch/N/$mute = the console's MUTE button state: 0 off, 1 own mute, 2 muted by a group.
    Pressing MUTE on a group-muted strip sets $mute 0 (override) while 'mute' stays 0.
    No push is sent for $mute changes -- poll it. OSC writes to $mute are IGNORED, so the page
    emulates the override by temporarily removing the group's '#Mn' tag (verified to unmute).
  * Input stage: /ch/N/in/set/trim (+-18 dB), in/set/inv (polarity), flt/lc|lcf|hc|hcf.
    Physical input /io/in/<grp>/<n>: g (preamp dB), vph (48V), pol, name, mode (M/ST).
    Re-patching pushes NOTHING -- routing must be polled.
  * Input routing: /ch/N/in/conn/grp ('A', 'LCL', 'B', ...) + /ch/N/in/conn/in (1-based).
  * Writes: fader / send levels take a FLOAT in dB (an int is ignored).
    Mutes and send on/off take an INT 0/1.
  * WING-LIVE SD recorder (verified Oct 2026, fw 3.1): /cards/wlive/{1,2}/$ctl/control
    accepts 'REC' / 'STOP' (string); 'PPAUSE' is ignored while recording (no record-pause) and
    REC while recording is a no-op. $ctl/setmarker = int 1 adds one marker and self-resets to 0.
    $stat/state|etime|sdfree|markers|markerlist|sessions are PUSHED via /*S (etime, sdfree in
    ms; list values push as text but a query returns [text, norm, index] -- read the text).
  * Main/Alt inputs: every channel has in/conn/grp+in (main) and in/conn/altgrp+altin (alt).
    /io/altsw (int 0/1) is the console-wide switch -- writable over OSC, pushed on change; each
    channel's in/set/altsrc (r/o) follows it (ch 1-40), and $name/$col follow the active source.
    /cards/wlive/auto_play|auto_rec|auto_stop (KEEP/MAIN/ALT, text) make the card flip it itself.
  * /io/out/USB/N/grp accepts MAIN BUS MTX AUX LCL A B ... (CH and DCA are not groups).
    /io/out/USB/N/in is 1-based on write (int or str); readback display string is 1-based.
"""
import socket
import struct
import threading
import time

PORT = 2223
NEG_INF = -144.0


# ── OSC encode / decode ─────────────────────────────────────────────────────────

def _pad(b):
    return b + b'\0' * (4 - len(b) % 4)


def osc_msg(addr, *args):
    tags, data = ',', b''
    for a in args:
        if isinstance(a, str):
            tags += 's'; data += _pad(a.encode())
        elif isinstance(a, bool) or isinstance(a, int):
            tags += 'i'; data += struct.pack('>i', int(a))
        else:
            tags += 'f'; data += struct.pack('>f', float(a))
    return _pad(addr.encode()) + _pad(tags.encode()) + data


def _rd_str(b, i):
    j = b.index(b'\0', i)
    return b[i:j].decode(errors='replace'), (j // 4 + 1) * 4


def osc_parse(b):
    addr, i = _rd_str(b, 0)
    if i >= len(b):
        return addr, []
    tags, i = _rd_str(b, i)
    out = []
    for t in tags[1:]:
        if t == 's':
            v, i = _rd_str(b, i)
        elif t == 'i':
            v = struct.unpack('>i', b[i:i + 4])[0]; i += 4
        elif t == 'f':
            v = struct.unpack('>f', b[i:i + 4])[0]; i += 4
        else:
            break
        out.append(v)
    return addr, out


# ── Address model ───────────────────────────────────────────────────────────────

N_CH, N_AUX, N_BUS, N_MTX, N_MAIN, N_MGRP = 40, 8, 16, 8, 2, 8
# Float leaves written in native units, clamped to the console's ranges
FLOAT_RANGES = {'g': (-2.5, 45.0), 'trim': (-18.0, 18.0), 'lcf': (20.0, 2000.0), 'hcf': (200.0, 20000.0)}
# Physical input groups and sizes (WING Rack fw 3.1, from /io/in '?')
SRC_GROUPS = [('LCL', 24), ('A', 48), ('B', 48), ('C', 48), ('SC', 32), ('USB', 48),
              ('CRD', 64), ('MOD', 64), ('PLAY', 4), ('AES', 2)]
SRC_LEAVES = ('name', 'mode', 'g', 'vph', 'pol')
KEEP_SHADOW = ('/$name', '/$solo', '/$col', '/$mute')     # '$' paths we do track (see _rx_loop)
N_SD = 2                                                     # WING-LIVE card: SD slots A, B
REC_TEXT = ('/state', '/control', '/sdstate', '/markerlist', '/sessionlist', '/errormessage', '/$type',
            '/auto_play', '/auto_rec', '/auto_stop')
AUTO_KEYS = ('auto_play', 'auto_rec', 'auto_stop')


def rec_addrs():
    """WING-LIVE recorder state the page shows (all '$' paths, all pushed when they change)."""
    a = ['/cards/$type', '/io/altsw'] + [f'/cards/wlive/{k}' for k in AUTO_KEYS]
    for n in range(1, N_SD + 1):
        b = f'/cards/wlive/{n}'
        a += [f'{b}/$ctl/control'] + [f'{b}/$stat/{k}' for k in
              ('state', 'etime', 'sdfree', 'sdsize', 'sdstate', 'markers', 'sessions', 'errormessage')]
    return a


def strip_addrs():
    """Every control address the mixer page reads (names, levels, mutes, sends)."""
    a = []
    for kind, n in (('ch', N_CH), ('aux', N_AUX)):
        for i in range(1, n + 1):
            a += [f'/{kind}/{i}/name', f'/{kind}/{i}/$name', f'/{kind}/{i}/fdr', f'/{kind}/{i}/mute',
                  f'/{kind}/{i}/$solo', f'/{kind}/{i}/$mute', f'/{kind}/{i}/in/conn/grp', f'/{kind}/{i}/in/conn/in',
                  f'/{kind}/{i}/col', f'/{kind}/{i}/$col', f'/{kind}/{i}/tags',
                  f'/{kind}/{i}/in/conn/altgrp', f'/{kind}/{i}/in/conn/altin', f'/{kind}/{i}/in/set/altsrc',
                  f'/{kind}/{i}/in/set/trim', f'/{kind}/{i}/in/set/inv',
                  f'/{kind}/{i}/flt/lc', f'/{kind}/{i}/flt/lcf', f'/{kind}/{i}/flt/hc', f'/{kind}/{i}/flt/hcf']
    for i in range(1, N_BUS + 1):
        a += [f'/bus/{i}/name', f'/bus/{i}/$name', f'/bus/{i}/fdr', f'/bus/{i}/mute',
              f'/bus/{i}/col', f'/bus/{i}/$col']
    for i in range(1, N_MAIN + 1):
        a += [f'/main/{i}/name', f'/main/{i}/$name', f'/main/{i}/fdr', f'/main/{i}/mute',
              f'/main/{i}/col', f'/main/{i}/$col']
    for i in range(1, N_MGRP + 1):
        a += [f'/mgrp/{i}/name', f'/mgrp/{i}/mute']
    for i in range(1, N_MTX + 1):
        a += [f'/mtx/{i}/name', f'/mtx/{i}/$name', f'/mtx/{i}/fdr', f'/mtx/{i}/mute']
    a += rec_addrs()
    # Sends last: the page is usable (LR mode) before these finish loading.
    for kind, n in (('ch', N_CH), ('aux', N_AUX)):
        for i in range(1, n + 1):
            for b in range(1, N_BUS + 1):
                a += [f'/{kind}/{i}/send/{b}/lvl', f'/{kind}/{i}/send/{b}/on']
    return a


def value_from_reply(addr, args):
    """Query reply [str, norm, native] -> native; single-arg push/string -> that arg.
    Routing '/in' params are kept 1-based (the display form) to match how they are written."""
    if not args:
        return None
    if addr.endswith(('/in', '/altin', '/col', '/$col')):     # 1-based palette / input index
        if len(args) >= 3 and str(args[0]).isdigit():
            return int(args[0])
        if isinstance(args[0], (int, float)):
            return int(args[0]) + 1
        return int(args[0]) if str(args[0]).isdigit() else args[0]
    if addr.startswith('/cards/') and addr.endswith(REC_TEXT):
        return args[0]                              # recorder lists: keep the text ('REC', 'READY')
    if len(args) >= 3:
        v = args[2]
    else:
        v = args[0]
    if isinstance(v, float):
        v = round(v, 2)
    return v


class Wing:
    """Owns one UDP socket: queries, writes, and the /*S push subscription."""

    def __init__(self, ip, on_update=None, on_conn=None, on_loaded=None):
        self.ip = ip
        self.on_update = on_update or (lambda a, v: None)
        self.on_conn = on_conn or (lambda ok: None)
        self.on_loaded = on_loaded or (lambda: None)
        self.state = {}
        self.lock = threading.Lock()
        self.connected = False
        self.loaded = False
        self.last_rx = 0.0
        self._waiters = {}            # addr -> threading.Event, for blocking query()
        self._raw = {}                # node addr -> [Event, text] for describe()
        self._raw_lock = threading.Lock()
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('', 0))
        self.sock.settimeout(0.5)

    # ── lifecycle ──
    def start(self):
        threading.Thread(target=self._rx_loop, daemon=True, name='wing-rx').start()
        threading.Thread(target=self._keepalive, daemon=True, name='wing-ka').start()

    def stop(self):
        self._stop.set()

    # ── io ──
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
                addr, args = osc_parse(data)
            except Exception:
                continue
            self.last_rx = time.time()
            if not self.connected:
                self.connected = True
                self.on_conn(True)
                threading.Thread(target=self._load_all, daemon=True, name='wing-load').start()
            raw = self._raw.get(addr)
            if raw and args and isinstance(args[0], str) and len(args) == 1 and '\n' in args[0]:
                raw[1] = args[0]; raw[0].set()           # node describe text -- not a value
                continue
            # '$' paths are read-only shadows (e.g. /ch/1/$fdr duplicates every fader push).
            # Keep '$name': it is the name the console actually displays (it can follow the
            # input source), while plain 'name' may hold a stale stored value.
            if '$' in addr and not addr.endswith(KEEP_SHADOW) and not addr.startswith('/cards/'):
                continue
            v = value_from_reply(addr, args)
            if v is None:
                continue
            with self.lock:
                changed = self.state.get(addr) != v
                self.state[addr] = v
                ev = self._waiters.pop(addr, None)
            if ev:
                ev.set()
            if changed:
                self.on_update(addr, v)

    def _keepalive(self):
        while not self._stop.is_set():
            self._send('/*S')
            self._send('/main/1/name')       # liveness probe; pushes only flow on change
            if self.connected and time.time() - self.last_rx > 15:
                self.connected = False
                self.loaded = False
                self.on_conn(False)
            self._stop.wait(5)

    def _load_all(self):
        for a in strip_addrs():
            if not self.connected or self._stop.is_set():
                return
            self._send(a)
            time.sleep(0.002)
        time.sleep(0.5)
        self.loaded = True
        self.on_loaded()

    # ── public api ──
    def query(self, addr, timeout=0.5):
        """Blocking read of one address (used for routing checks)."""
        ev = threading.Event()
        with self.lock:
            self._waiters[addr] = ev
        self._send(addr)
        if ev.wait(timeout):
            with self.lock:
                return self.state.get(addr)
        with self.lock:
            self._waiters.pop(addr, None)
        return None

    def set(self, addr, value):
        """Write a value with the type the WING expects; update cache optimistically."""
        leaf = addr.rsplit('/', 1)[-1]
        if leaf in ('fdr', 'lvl'):
            v = round(max(NEG_INF, min(10.0, float(value))), 2)
            self._send(addr, float(v))
        elif leaf in FLOAT_RANGES:
            lo, hi = FLOAT_RANGES[leaf]
            v = round(max(lo, min(hi, float(value))), 2)
            self._send(addr, float(v))
        elif leaf == 'tags':
            v = str(value).encode()[:80].decode(errors='ignore')
            self._send(addr, v)
        elif leaf in ('mute', 'on', '$solo', 'inv', 'vph', 'pol', 'lc', 'hc'):
            v = 1 if int(value) else 0
            self._send(addr, v)
        elif leaf in ('grp',):
            v = str(value); self._send(addr, v)
        elif leaf in ('in',):
            v = int(value); self._send(addr, v)
        elif addr == '/io/altsw':
            v = 1 if int(value) else 0
            self._send(addr, v)
        elif leaf in AUTO_KEYS and addr.startswith('/cards/wlive/'):
            v = str(value).upper()
            if v not in ('KEEP', 'MAIN', 'ALT'):
                return None
            self._send(addr, v)
        elif leaf == 'control' and addr.startswith('/cards/wlive/'):
            v = str(value).upper()
            if v not in ('REC', 'STOP'):
                return None
            self._send(addr, v)
        elif leaf == 'setmarker' and addr.startswith('/cards/wlive/'):
            self._send(addr, 1)
            return 1                                # self-resetting trigger: don't cache it
        else:
            return None
        with self.lock:
            self.state[addr] = v
        return v

    def query_many(self, addrs, timeout=1.0):
        """Fire a batch of queries, wait (bounded) for the replies, return {addr: value}."""
        evs = {}
        with self.lock:
            for a in addrs:
                evs[a] = self._waiters[a] = threading.Event()
        for a in addrs:
            self._send(a); time.sleep(0.001)
        end = time.time() + timeout
        for a, ev in evs.items():
            ev.wait(max(0.0, end - time.time()))
        with self.lock:
            for a in addrs:
                self._waiters.pop(a, None)
            return {a: self.state.get(a) for a in addrs}

    def describe(self, node, timeout=1.0):
        """Node description with values ('#'): one line per parameter. Serialized per node."""
        with self._raw_lock:
            slot = [threading.Event(), None]
            self._raw[node] = slot
            self._send(node, '#')
            slot[0].wait(timeout)
            self._raw.pop(node, None)
            return slot[1]

    def poke(self, addrs, pace=0.001):
        """Fire-and-forget refresh: replies update the cache (and push changes) via _rx_loop."""
        for a in addrs:
            self._send(a); time.sleep(pace)

    def get(self, addr, default=None):
        with self.lock:
            return self.state.get(addr, default)

    def snapshot(self):
        with self.lock:
            return dict(self.state)


# ── node describe parsing ('#') ─────────────────────────────────────────────────
# e.g. "    thr            -28.0               lin [-80.0 .. 0.0 dB], 161 steps"
#      "    mix            100             r/o lin [0 .. 100 %], 101 steps"
#      "    ratio          gate                list [1:1.5, 1:2, 1:3, 1:4, gate]"
import re as _re
_DESC = _re.compile(r'^\s*(\S+)\s+(.*?)\s+(r/o\s+)?(int|lin|log|list|string)\b\s*(.*)$')


def wing_num(txt):
    """'2k89' -> 2890.0, '+8.3' -> 8.3, '11k74' -> 11740.0"""
    t = str(txt).strip().replace('+', '')
    m = _re.match(r'^(-?\d+)k(\d*)$', t)
    if m:
        return float(f'{m.group(1)}.{m.group(2) or 0}') * 1000
    return float(t)


def parse_describe(text):
    out = []
    for line in (text or '').splitlines():
        m = _DESC.match(line)
        if not m:
            continue
        key, val, ro, typ, rest = m.groups()
        if key.startswith('$'):
            continue
        p = {'key': key, 'type': typ, 'ro': bool(ro)}
        try:
            if typ == 'list':
                opts = rest[rest.index('[') + 1:rest.rindex(']')]
                p['opts'] = [o.strip() for o in opts.split(',')]
                p['value'] = val.strip().strip("'")
            elif typ in ('lin', 'log', 'int'):
                rng = rest[rest.index('[') + 1:rest.index(']')]
                lo, hi = [x.strip() for x in rng.split('..')]
                hi_parts = hi.split()
                p['lo'], p['hi'] = wing_num(lo.split()[0]), wing_num(hi_parts[0])
                p['unit'] = hi_parts[1] if len(hi_parts) > 1 else ''
                sm = _re.search(r'(\d+)\s+steps', rest)
                p['steps'] = int(sm.group(1)) if sm else None
                p['value'] = wing_num(val)
            else:
                p['value'] = val.strip().strip("'")
        except (ValueError, IndexError):
            continue
        out.append(p)
    return out
