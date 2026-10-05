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
                    a += [f'{rb}/config/source', f'/-stat/solosw/{solo_index(kind, n):02d}']
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

    def _d_source(self, kind, n):
        cb = f'/{kind}/{n}'
        g, i = self._source(kind, n, False)
        ag, ai = self._source(kind, n, True)
        if g is None:
            return []
        return [(cb + '/in/conn/grp', g), (cb + '/in/conn/in', i if i is not None else 0),
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
        sp = self._split(addr)
        if not sp:
            return None
        kind, n, leaf = sp
        rb = raw_base(kind, n)
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
