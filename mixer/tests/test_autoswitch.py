"""
Console auto-detect + hot swap (v3.3): no IP configured, the Pi finds whichever console answers and
follows it when it changes -- X32 -> WING -> WING at a new address -- without a restart.
Fake consoles on loopback aliases 127.0.0.2 / 127.0.0.3 (Linux answers all of 127/8); discovery is
pointed at them with mixer_scan (the real Pi searches eth0's broadcast + subnet instead).

    cd ~/stage-messenger && python3 -m mixer.tests.test_autoswitch
"""
import ipaddress
import json
import os
import sys
import time

from mixer.tests.test_x32_api import check, wait_for, setup, FAILS
from mixer.tests.fake_x32 import FakeX32
from mixer.tests.test_wing_regression import FakeWing

A, B = '127.0.0.2', '127.0.0.3'


def drain(q):
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


def stop_x32(f):
    f.stop(); time.sleep(0.3); f.s.close()


def main():
    tmp, mixer, mx, c = setup({'mixer_type': 'auto', 'mixer_iface': '', 'mixer_scan': [A, B], 'watch_s': 0.5,
                               'spotify': {'enabled': False}, 'rtc': {'enabled': False}},
                              state={'console': 'x32', 'order_x32': ['ch/5'], 'order': ['ch/40']})
    # fast liveness so the test doesn't wait 15-20 s per swap (real values: WING 5/15 s, X32 8/20 s)
    for D in (mixer.wing.Wing, mixer.x32.X32):
        D.KA, D.TIMEOUT = 0.4, 1.5
    q = mx.hub.subscribe()
    fx = fw = fw2 = fx2 = None
    try:
        print('nothing on the network')
        check('no console: searching, no address', not mx.found and mx.ip == '' and not mx.wing.connected)
        st = c.get('/mixer/api/state').get_json()
        check('snapshot found=false (page: "Looking for a console")', st['found'] is False and st['ip'] == '', st.get('found'))
        check('layout = last console (x32) while searching', st['caps']['console'] == 'x32')

        print('M32C appears at .2')
        fx = FakeX32(host=A).start()
        check('found + swapped to x32 @ .2', wait_for(lambda: mx.console == 'x32' and mx.ip == A and mx.wing.loaded, 12),
              (mx.console, mx.ip))
        check('caps from /xinfo (M32C 4.06-8)', mx.caps['model'] == 'M32C' and mx.caps['fw'] == '4.06-8', mx.caps)
        check('X32 channel order kept', mx.order == ['ch/5'], mx.order)
        snaps = [m for m in drain(q) if m['t'] == 'snap']
        check('pages told: snap with M32C caps', any(m['caps']['model'] == 'M32C' for m in snaps))
        check('page served M32C CAPS', '"model":"M32C"' in c.get('/mixer').get_data(as_text=True))
        r = c.post('/mixer/api/set', json={'a': '/ch/3/fdr', 'v': -10}).get_json()
        check('fader write reaches the M32C', r['ok'] and wait_for(lambda: abs(fx.st['/ch/03/mix/fader'] - 0.5) < 0.01, 2),
              fx.st.get('/ch/03/mix/fader'))
        sf = json.load(open(os.path.join(tmp, 'mixer_state.json')))
        check('remembered: x32 @ .2', sf.get('console') == 'x32' and sf.get('console_ip') == A, sf)

        print('swap cases: M32C out, WING in at .3')
        old = mx.wing
        stop_x32(fx); fx = None
        fw = FakeWing(host=B)
        check('swapped to WING @ .3', wait_for(lambda: mx.console == 'wing' and mx.ip == B and mx.wing.loaded, 15),
              (mx.console, mx.ip))
        check('old X32 driver stopped + socket freed', old._stop.is_set() and wait_for(lambda: old.sock.fileno() == -1, 2))
        check('WING caps (40 ch, listen)', mx.caps['nch'] == 40 and mx.caps['listen'] and mx.caps['model'] == 'WING')
        check('WING channel order loaded', mx.order == ['ch/40'], mx.order)
        check('WING meters driver', type(mx.meters).__name__ == 'Meters')
        snaps = [m for m in drain(q) if m['t'] == 'snap']
        check('pages told: snap with WING caps', any(m['caps']['console'] == 'wing' for m in snaps))
        n = len(fw.writes)
        r = c.post('/mixer/api/set', json={'a': '/ch/7/fdr', 'v': -3}).get_json()
        check('fader write reaches the WING', r['ok'] and wait_for(lambda: ('/ch/7/fdr', -3.0) in fw.writes[n:], 2))
        check('USB patch written on the WING', wait_for(lambda: any(a.startswith('/io/out/USB/') for a, _ in fw.writes), 4))
        sf = json.load(open(os.path.join(tmp, 'mixer_state.json')))
        check('remembered: wing @ .3, X32 order kept', sf.get('console') == 'wing' and sf.get('console_ip') == B
              and sf.get('order_x32') == ['ch/5'], sf)
        r = c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 2}).get_json()
        check('WING mute path live after swap', r['ok'] and r['action'] == 'own', r)

        print('DHCP move: WING now at .2')
        fw.stop(); fw = None
        fw2 = FakeWing(host=A)
        check('followed the WING to .2', wait_for(lambda: mx.console == 'wing' and mx.ip == A and mx.wing.loaded, 15),
              (mx.console, mx.ip))

        print('a connected console is never second-guessed')
        fx2 = FakeX32(host=B).start()
        time.sleep(3)
        check('still WING @ .2 with an M32C also answering', mx.console == 'wing' and mx.ip == A and mx.wing.connected)

        sr = dict(mx._state_raw)
        check('both answer -> the one used last (WING @ .2)', (lambda f: f and (f['kind'], f['ip']) == ('wing', A))(mx._find(sweep=False)))
        mx._state_raw.update({'console': 'x32', 'console_ip': B})
        check('both answer, last used M32C @ .3 -> M32C', (lambda f: f and (f['kind'], f['ip']) == ('x32', B))(mx._find(sweep=False)))
        mx._state_raw.update({'console': 'x32', 'console_ip': '10.9.9.9'})
        check('last used type wins when its address changed', (lambda f: f and f['kind'] == 'x32')(mx._find(sweep=False)))
        mx._state_raw = sr

        print('v4.2 console choice (page: Auto / WING / X32) with both answering')
        st = c.get('/mixer/api/state').get_json()
        check('snapshot carries the choice (auto, not pinned)', st.get('cpref', {}).get('v') == 'auto'
              and st['cpref']['pinned'] is False, st.get('cpref'))
        drain(q)
        r = c.post('/mixer/api/console/pref', json={'pref': 'x32'})
        check('choose X32 -> ok', r.status_code == 200 and r.get_json()['cpref']['v'] == 'x32', r.get_json())
        check('switched to the M32C @ .3 right away', wait_for(lambda: mx.console == 'x32' and mx.ip == B and mx.wing.loaded, 8),
              (mx.console, mx.ip))
        check('pages told (cpref + snap with M32C caps)', (lambda ms: any(m['t'] == 'cpref' for m in ms)
              and any(m['t'] == 'snap' and m['caps']['console'] == 'x32' for m in ms))(drain(q)))
        sf = json.load(open(os.path.join(tmp, 'mixer_state.json')))
        check('choice saved in mixer_state.json', sf.get('console_pref') == 'x32', sf.get('console_pref'))
        time.sleep(2.5)
        check('watcher keeps the chosen M32C (WING still answering)', mx.console == 'x32' and mx.ip == B)
        r = c.post('/mixer/api/console/pref', json={'pref': 'wing'})
        check('choose WING -> back to the WING @ .2', r.status_code == 200 and
              wait_for(lambda: mx.console == 'wing' and mx.ip == A and mx.wing.loaded, 8), (mx.console, mx.ip))
        r = c.post('/mixer/api/console/pref', json={'pref': 'auto'})
        time.sleep(2)
        check('Auto: the connected WING is kept', r.status_code == 200 and mx.console == 'wing' and mx.ip == A
              and mx.console_pref()['v'] == 'auto', (mx.console, mx.ip))
        stop_x32(fx2); fx2 = None
        r = c.post('/mixer/api/console/pref', json={'pref': 'x32'})
        check('X32 chosen but none answering: stays on the WING with a note',
              wait_for(lambda: 'still looking' in mx.console_pref()['note'], 6) and mx.console == 'wing', mx.console_pref())
        fx2 = FakeX32(host=B).start()
        check('watcher switches when the chosen X32 shows up', wait_for(lambda: mx.console == 'x32' and mx.ip == B
              and mx.wing.loaded, 12), (mx.console, mx.ip))
        check('note cleared after the switch', mx.console_pref()['note'] == '', mx.console_pref())
        r = c.post('/mixer/api/console/pref', json={'pref': 'mackie'})
        check('bad choice -> 400', r.status_code == 400)
        mx.pinned = '10.1.1.1'
        r = c.post('/mixer/api/console/pref', json={'pref': 'wing'})
        check('pinned IP -> 409, choice unchanged', r.status_code == 409 and mx.console_pref()['v'] == 'x32')
        mx.pinned = ''
        c.post('/mixer/api/console/pref', json={'pref': 'wing'})
        check('back on the WING @ .2 for the rest', wait_for(lambda: mx.console == 'wing' and mx.ip == A and mx.wing.loaded, 8))
        check("mixer_type x32 in config, page says auto -> auto", mx._pref_want('auto') is None
              and mx._pref_want(None) == mx.cfg_want and mx._pref_want('wing') == 'wing')

        print('discovery')
        Finder = mixer.discover.Finder
        got = Finder('', [A, B], timeout=0.5).discover(sweep=False)
        kinds = sorted((g['kind'], g['ip']) for g in got)
        check('both consoles answer', kinds == [('wing', A), ('x32', B)], kinds)
        g = Finder('', [A, B], timeout=0.5).discover(sweep=False, want='x32')
        check('mixer_type x32 filters out the WING', [x['ip'] for x in g] == [B], g)
        w = [x for x in got if x['kind'] == 'wing'][0]
        check('WING model ids made readable', mixer.discover.parse_reply(b'WING,1.2.3.4,x,wing-rack,S,3.1', 2222)['model'] == 'WING Rack'
              and mixer.discover.parse_reply(b'WING,1.2.3.4,x,ngc-full,S,3.1', 2222)['model'] == 'WING')
        check('WING reply parsed (name/model/fw)', w['name'] == 'Josephs-Wing' and w['model'] == 'WING' and w['fw'] == '3.1', w)
        check('unanswered address -> nothing (no error)', Finder('', ['127.0.0.9'], timeout=0.3).discover(sweep=False) == [])
        check('no interface address -> nothing', Finder('nonexistent0', [], timeout=0.2).discover() == [])
        fx3 = FakeX32(host='127.0.0.77').start()
        orig = mixer.discover.iface_net
        mixer.discover.iface_net = lambda i: (('127.0.0.1', ipaddress.IPv4Interface('127.0.0.1/24'))
                                              if i == 'fake0' else orig(i))
        try:
            t = time.time()
            g = Finder('fake0', timeout=0.5).discover()
            dt = time.time() - t
            check('subnet sweep finds what broadcast missed (in < 2.5 s)', ('x32', '127.0.0.77') in
                  [(x['kind'], x['ip']) for x in g] and dt < 2.5, (g, round(dt, 2)))
            check('no sweep when asked not to', Finder('fake0', timeout=0.4).discover(sweep=False) == [])
            g = Finder('fake0', [A], timeout=0.5).discover(want='x32')
            check('v4.2: only the other type answered the broadcast -> still sweeps for the wanted one',
                  ('x32', '127.0.0.77') in [(x['kind'], x['ip']) for x in g] and all(x['kind'] == 'x32' for x in g), g)
        finally:
            mixer.discover.iface_net = orig
            stop_x32(fx3)
        check('reply parser rejects junk', mixer.discover.parse_reply(b'hello', 2222) is None
              and mixer.discover.parse_reply(b'\x00\x01', 10023) is None)
    finally:
        mx.wing.stop()
        for f in (fw, fw2):
            if f:
                f.stop()
        for f in (fx, fx2):
            if f:
                stop_x32(f)
    print()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
