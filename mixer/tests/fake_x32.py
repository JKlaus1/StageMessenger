"""
Stateful fake X32 / M32 (UDP) for off-hardware tests -- behaviour from the Oct 2026 M32C probes.

  * queries return the stored value; writes store it (the real console doesn't echo a write back to
    the sender, so neither does this fake -- only OTHER clients holding an /xremote lease get pushed)
  * mute groups: engaging sets every member's mix/on 0, releasing sets every member's mix/on 1
    (including strips that were muted on their own -- like the M32), pushed to /xremote clients
  * /-action/clearsolo, /xinfo, /meters/0|1|2 blobs (~20/s for 10 s)
  * console_set(addr, v) simulates someone at the desk (pushes to every /xremote client)

    python3 -m mixer.tests.fake_x32 [port]        (standalone, default 10023)
"""
import socket
import struct
import sys
import threading
import time


def _pad(b):
    return b + b'\0' * (4 - len(b) % 4)


def msg(addr, *args):
    tags, data = ',', b''
    for a in args:
        if isinstance(a, bytes):
            tags += 'b'; data += struct.pack('>i', len(a)) + a + b'\0' * ((4 - len(a) % 4) % 4)
        elif isinstance(a, str):
            tags += 's'; data += _pad(a.encode())
        elif isinstance(a, int):
            tags += 'i'; data += struct.pack('>i', a)
        else:
            tags += 'f'; data += struct.pack('>f', a)
    return _pad(addr.encode()) + _pad(tags.encode()) + data


def parse(b):
    def rd(i):
        j = b.index(b'\0', i)
        return b[i:j].decode(), (j // 4 + 1) * 4
    a, i = rd(0)
    if i >= len(b):
        return a, []
    t, i = rd(i)
    out = []
    for c in t[1:]:
        if c == 's':
            v, i = rd(i)
        elif c == 'i':
            v = struct.unpack('>i', b[i:i + 4])[0]; i += 4
        else:
            v = struct.unpack('>f', b[i:i + 4])[0]; i += 4
        out.append(v)
    return a, out


def seed():
    """Close to the probed 'MyShow' (ch 1 Kick BL, mute groups 1+2 on most channels, buses 13-16 in MG1)."""
    st = {'/config/routing/routswitch': 0,
          '/config/routing/IN/1-8': 4, '/config/routing/IN/9-16': 5, '/config/routing/IN/17-24': 4,
          '/config/routing/IN/25-32': 12}
    for b in ('1-8', '9-16', '17-24', '25-32'):
        st[f'/config/routing/PLAY/{b}'] = 0
    for u in range(1, 33):
        st[f'/config/userrout/in/{u:02d}'] = 0
    for u in range(1, 49):                   # v3.9 listen block: user outs all OFF, card blocks as probed
        st[f'/config/userrout/out/{u:02d}'] = 0
    st.update({'/config/routing/CARD/1-8': 0, '/config/routing/CARD/9-16': 1,
               '/config/routing/CARD/17-24': 25, '/config/routing/CARD/25-32': 26})
    for g in range(1, 7):
        st[f'/config/mute/{g}'] = 0
    names = {1: 'Kick', 2: 'SNARE', 14: 'Bass', 18: 'Singer 1', 21: 'Singer 3', 22: 'TRACK'}
    for i in range(1, 33):
        b = f'/ch/{i:02d}'
        st[b + '/config/name'] = names.get(i, '')
        st[b + '/config/color'] = {1: 4, 2: 2, 18: 15}.get(i, 0)
        st[b + '/config/source'] = i
        st[b + '/mix/fader'] = {1: 0.567937, 2: 0.0, 14: 0.517107}.get(i, 0.75)
        st[b + '/mix/on'] = 0 if i == 21 else 1
        st[b + '/grp/mute'] = 0 if i in (17, 22, 23, 24, 25, 26) else 3
        for s in range(1, 17):
            st[f'{b}/mix/{s:02d}/level'] = 0.3875 if (i, s) == (1, 1) else 0.0
            st[f'{b}/mix/{s:02d}/on'] = 1
    for i in range(1, 9):
        b = f'/auxin/{i:02d}'
        st.update({b + '/config/name': '', b + '/config/color': 0, b + '/config/source': 32 + min(i, 6),
                   b + '/mix/fader': 0.0, b + '/mix/on': 0, b + '/grp/mute': 0})
        for s in range(1, 17):
            st[f'{b}/mix/{s:02d}/level'] = 0.0
            st[f'{b}/mix/{s:02d}/on'] = 1
    for i in range(1, 17):
        b = f'/bus/{i:02d}'
        st.update({b + '/config/name': {1: 'Gtr ', 13: 'vox1'}.get(i, ''), b + '/config/color': 7,
                   b + '/mix/fader': 0.749756, b + '/mix/on': 1, b + '/grp/mute': 7 if i in (13, 14) else (3 if i in (15, 16) else 0)})
    for i in range(1, 7):
        b = f'/mtx/{i:02d}'
        st.update({b + '/config/name': 'MatrixBus', b + '/config/color': 3, b + '/mix/fader': 0.254154, b + '/mix/on': 1})
    for b, nm in (('/main/st', 'Main Array'), ('/main/m', '')):
        st.update({b + '/config/name': nm, b + '/config/color': 7 if nm else 0, b + '/mix/fader': 0.749756, b + '/mix/on': 0})
    for k in range(1, 81):
        st[f'/-stat/solosw/{k:02d}'] = 0
    for pre, cnt in (('/ch', 32), ('/auxin', 8), ('/bus', 16)):          # v4.0.3: DCA membership (ch 1-2 in DCA 1)
        for i in range(1, cnt + 1):
            st[f'{pre}/{i:02d}/grp/dca'] = 1 if (pre, i) in (('/ch', 1), ('/ch', 2)) else 0
    # v4.0 console view: DCAs, pan, LR / M/C assigns, matrix sends (bus / main -> mtx 1-6)
    for i in range(1, 9):
        st.update({f'/dca/{i}/fader': 0.75 if i < 3 else 0.0, f'/dca/{i}/on': 1,
                   f'/dca/{i}/config/name': {1: 'Drums', 2: 'Vox'}.get(i, ''), f'/dca/{i}/config/color': 2 if i == 1 else 0})
    for pre, cnt in (('/ch', 32), ('/auxin', 8), ('/bus', 16)):
        for i in range(1, cnt + 1):
            b = f'{pre}/{i:02d}'
            st.update({b + '/mix/pan': 0.5, b + '/mix/st': 1, b + '/mix/mono': 0, b + '/mix/mlevel': 0.0})
    for b in [f'/bus/{i:02d}' for i in range(1, 17)] + ['/main/st', '/main/m']:
        for m in range(1, 7):
            st[f'{b}/mix/{m:02d}/level'] = 0.75 if (b, m) == ('/bus/01', 2) else 0.0
            st[f'{b}/mix/{m:02d}/on'] = 1
    st['/-stat/solo'] = 0
    # v3.1: input stage, headamps (no stagebox: every /-ha index -1), processing = probed Ch 1 values
    eq = {'on': 1}
    for b, (t, f, g, q) in enumerate(((2, .155, .6, .464789), (2, .465, .141667, .802817),
                                       (2, .05, .508333, .056338), (3, .79, .675, .43662)), 1):
        eq.update({f'{b}/type': t, f'{b}/f': f, f'{b}/g': g, f'{b}/q': q})
    gate = {'on': 1, 'mode': 3, 'thr': .625, 'range': 1.0, 'attack': 0.0, 'hold': .71, 'release': .51, 'keysrc': 0,
            'filter/on': 0, 'filter/type': 4, 'filter/f': .16}
    dyn = {'on': 0, 'mode': 0, 'det': 0, 'env': 0, 'thr': .65, 'ratio': 2, 'knee': .2, 'mgain': .270833,
           'attack': .05, 'hold': .69, 'release': .54, 'pos': 1, 'keysrc': 0, 'mix': 1.0, 'auto': 0,
           'filter/on': 0, 'filter/type': 5, 'filter/f': .45}
    for i in range(1, 33):
        b = f'/ch/{i:02d}'
        st.update({b + '/preamp/trim': .916667 if i == 1 else .5, b + '/preamp/invert': 0, b + '/preamp/hpon': 1,
                   b + '/preamp/hpf': .28, b + '/preamp/hpslope': 2})
        st.update({f'{b}/eq/{k}': v for k, v in eq.items()})
        st.update({f'{b}/gate/{k}': v for k, v in gate.items()})
        st.update({f'{b}/dyn/{k}': v for k, v in dyn.items()})
    eq6 = {'on': 1}                               # v4.0.3: output strips -- 6-band EQ + dyn
    for b6 in range(1, 7):
        eq6.update({f'{b6}/type': 2, f'{b6}/f': .1 + b6 * .12, f'{b6}/g': .5, f'{b6}/q': .5})
    eq6['1/type'] = 0                             # band 1 = low cut
    for b in [f'/bus/{i:02d}' for i in range(1, 17)] + [f'/mtx/{i:02d}' for i in range(1, 7)] + ['/main/st', '/main/m']:
        st.update({f'{b}/eq/{k}': v for k, v in eq6.items()})
        st.update({f'{b}/dyn/{k}': v for k, v in dyn.items()})
    for i in range(1, 9):
        b = f'/auxin/{i:02d}'
        st.update({b + '/preamp/trim': .555556, b + '/preamp/invert': 0})
        st.update({f'{b}/eq/{k}': v for k, v in eq.items()})
    for k in range(40):
        st[f'/-ha/{k:02d}/index'] = -1
    for h in range(128):
        st[f'/headamp/{h:03d}/gain'] = .381944 if h == 32 else .284722
        st[f'/headamp/{h:03d}/phantom'] = 1 if h == 32 else 0
    # v3.2: X-LIVE with an SD card in slot 1 (probe 2/3 values); sessions/markers live in FakeX32.sessions
    st.update({'/-stat/xcardtype': 10, '/-prefs/card/URECsdsel': 0, '/-urec/sd1state': 1, '/-urec/sd2state': 0,
               '/-urec/sd1info': '31 GB - 1h, 23m, 47s ', '/-urec/sd2info': 'Insert SD Card.', '/-urec/errorcode': 6,
               '/-urec/errormessage': 'System error: 6', '/-stat/urec/state': 0, '/-stat/urec/etime': 0,
               '/-stat/urec/rtime': 9189, '/-urec/sessionmax': 2, '/-urec/sessionpos': 2, '/-urec/sessionlen': 17280,
               '/-urec/markermax': 2, '/-urec/markerpos': 0})
    return st


class FakeX32:
    def __init__(self, port=10023, host='127.0.0.1', drop=()):
        self.st = seed()
        self.writes = []                     # (addr, value) from clients
        self.peers = {}                      # peer -> lease expiry
        self.drop = set(drop)                # addresses that never answer (UDP-loss test)
        self.meter_level = {0: 0.1, 48: 0.1}  # index in /meters/0 -> linear value (others floor)
        self.sessions = [{'name': '2026/10/05 | 10:53:06   ', 'len': 9189, 'marks': [3970, 6050, 8450]},
                         {'name': '2026/10/05 | 11:07:22   ', 'len': 17280, 'marks': [3655, 9495]}]
        self.actions = []                    # (/-action/..., value) in order
        self._rec_t0 = None
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind((host, port))
        self.s.settimeout(0.2)
        self.port = self.s.getsockname()[1]
        self._stop = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        for i, sess in enumerate(self.sessions, 1):
            self.st[f'/-urec/session/{i:03d}/name'] = sess['name']
        for i, t in enumerate(self.sessions[1]['marks'], 1):
            self.st[f'/-urec/marker/{i:03d}/time'] = t
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _push(self, addr, v, skip=None):
        now = time.time()
        for p, exp in list(self.peers.items()):
            if exp > now and p != skip:
                try:
                    self.s.sendto(msg(addr, v), p)
                except OSError:
                    pass

    def _store(self, addr, v, src=None):
        """Store + console side effects; push to every lease holder except the writer."""
        with self.lock:
            self.st[addr] = v
            effects = []
            if addr.startswith('/config/mute/'):
                g = int(addr.rsplit('/', 1)[1])
                for k in list(self.st):
                    if k.endswith('/grp/mute') and self.st[k] & (1 << (g - 1)):
                        on = k[:-len('/grp/mute')] + '/mix/on'
                        nv = 0 if v else 1
                        if self.st.get(on) != nv:
                            self.st[on] = nv; effects.append((on, nv))
            if addr.startswith('/-stat/solosw/'):
                any_on = int(any(self.st[f'/-stat/solosw/{k:02d}'] for k in range(1, 81)))
                if self.st['/-stat/solo'] != any_on:
                    self.st['/-stat/solo'] = any_on; effects.append(('/-stat/solo', any_on))
        self._push(addr, v, skip=src)
        for a, nv in effects:                      # side effects go to everyone, writer included
            self._push(a, nv)

    def console_set(self, addr, v):
        self._store(addr, v)

    # ── X-LIVE ──
    def _cur(self):
        p = self.st['/-urec/sessionpos']
        return self.sessions[p - 1] if 1 <= p <= len(self.sessions) else None

    def _show(self, sess):
        """Publish a session's length + marker list the way the M32 does (pushed to /xremote clients)."""
        old = self.st['/-urec/markermax']
        for i, t in enumerate(sess['marks'] if sess else [], 1):
            self.st[f'/-urec/marker/{i:03d}/time'] = t; self._push(f'/-urec/marker/{i:03d}/time', t)
        for i in range(len(sess['marks']) + 1 if sess else 1, old + 1):
            self.st[f'/-urec/marker/{i:03d}/time'] = 0
        self._store('/-urec/markermax', len(sess['marks']) if sess else 0)
        self._store('/-urec/sessionlen', sess['len'] if sess else 0)

    def _urec_state(self, v, src):
        cur = self.st['/-stat/urec/state']
        if cur == 3 and v == 1:                    # no record-pause: ignored, state re-pushed
            self._push('/-stat/urec/state', 3); return
        if v == 3 and cur != 3:
            self._rec_t0 = time.time(); self.rec_marks = []
            self._store('/-urec/sessionpos', 0); self._store('/-urec/sessionlen', 0)
        if cur == 3 and v == 0:                    # STOP after REC: new session, opened
            length = int((time.time() - (self._rec_t0 or time.time())) * 1000) + 1000
            self.sessions.append({'name': time.strftime('%Y/%m/%d | %H:%M:%S   '), 'len': length, 'marks': list(self.rec_marks)})
            n = len(self.sessions)
            self.st[f'/-urec/session/{n:03d}/name'] = self.sessions[-1]['name']
            self._push(f'/-urec/session/{n:03d}/name', self.sessions[-1]['name'])
            self._store('/-urec/sessionmax', n); self._store('/-urec/sessionpos', n); self._show(self.sessions[-1])
        if v == 0:
            self._store('/-stat/urec/etime', 0)
        self._store('/-stat/urec/state', v, src=src)

    def _action(self, a, v):
        self.actions.append((a, v))
        name = a.rsplit('/', 1)[1]
        if name == 'addmarker':
            at = int((time.time() - self._rec_t0) * 1000) if self.st['/-stat/urec/state'] == 3 else self.st['/-stat/urec/etime']
            if self.st['/-stat/urec/state'] == 3:
                self.rec_marks.append(at); self._store('/-urec/markermax', len(self.rec_marks))
            elif self._cur():
                self._cur()['marks'] = sorted(self._cur()['marks'] + [at]); self._show(self._cur())
        elif name == 'selsession' and 1 <= v <= len(self.sessions):
            self._store('/-urec/sessionpos', v); self._show(self.sessions[v - 1])
        elif name == 'selmarker' and self._cur() and 1 <= v <= len(self._cur()['marks']):
            self._store('/-urec/markerpos', v); self._store('/-stat/urec/etime', self._cur()['marks'][v - 1])
        elif name == 'delmarker' and self._cur() and 1 <= v <= len(self._cur()['marks']):
            del self._cur()['marks'][v - 1]; self._show(self._cur())
        elif name == 'setposition' and isinstance(v, int) and v >= 0:     # a float is ignored, like the M32
            self._store('/-stat/urec/etime', v)
        self._push(a, 0)                           # the console echoes the action reset to 0

    def _meters(self, bank, peer):
        n = {'/meters/0': 70, '/meters/1': 96, '/meters/2': 49}.get(bank)
        end = time.time() + 10
        while n and time.time() < end and not self._stop.is_set():
            if bank == '/meters/0':
                vals = [self.meter_level.get(i, 3.98e-7) for i in range(n)]
            elif bank == '/meters/1':
                vals = [self.meter_level.get(i, 3.98e-7) if i < 32 else 1.0 for i in range(n)]
            else:
                vals = [self.meter_level.get(48 + i, 3.98e-7) if i < 16 else (1.0 if i >= 25 else 3.98e-7) for i in range(n)]
            try:
                self.s.sendto(msg(bank, struct.pack('<i', n) + struct.pack(f'<{n}f', *vals)), peer)
            except OSError:
                return
            time.sleep(0.05)

    def _loop(self):
        while not self._stop.is_set():
            try:
                d, peer = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                a, args = parse(d)
            except Exception:
                continue
            if a == '/xremote':
                self.peers[peer] = time.time() + 10
            elif a == '/xinfo':
                self.s.sendto(msg('/xinfo', '192.168.0.43', 'M32C-08-7C-3B', 'M32C', '4.06-8'), peer)
            elif a == '/meters':
                threading.Thread(target=self._meters, args=(args[0], peer), daemon=True).start()
            elif a == '/-action/clearsolo':
                self.writes.append((a, args[0] if args else None))
                for k in range(1, 81):
                    if self.st[f'/-stat/solosw/{k:02d}']:
                        self._store(f'/-stat/solosw/{k:02d}', 0)
            elif a.startswith('/-action/') and args:
                self.writes.append((a, args[0]))
                self._action(a, args[0])
            elif a == '/-stat/urec/state' and args:
                self.writes.append((a, args[0]))
                self._urec_state(int(args[0]), peer)
            elif args:
                self.writes.append((a, args[0]))
                if a in self.st:
                    self._store(a, args[0], src=peer)
            elif a in self.st and a not in self.drop:
                self.s.sendto(msg(a, self.st[a]), peer)


if __name__ == '__main__':
    f = FakeX32(int(sys.argv[1]) if len(sys.argv) > 1 else 10023, host='0.0.0.0').start()
    print(f'fake M32C on udp {f.port}', flush=True)
    while True:
        time.sleep(1)
