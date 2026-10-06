"""
WING path regression (v3.0): with mixer_type 'wing' the controller must behave exactly as before the
X32 work -- WING driver, 40/8/16 strips, USB patch ownership, own-mute toggle, float fader writes,
the WING channel order key. Minimal fake WING on 127.0.0.1:2223 (query reply [display, norm, native]).

    cd ~/stage-messenger && python3 -m mixer.tests.test_wing_regression
"""
import json
import os
import socket
import sys
import threading
import time

from mixer.tests.test_x32_api import check, wait_for, setup, FAILS
from mixer.tests.fake_x32 import msg, parse


class FakeWing:
    """OSC on host:2223, plus discovery ('WING?' on host:2222 -> 'WING,ip,name,model,serial,fw')."""

    def __init__(self, host='127.0.0.1'):
        self.host = host
        self.st = {}
        self.writes = []
        self.disc = 0
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind((host, 2223))
        self.s.settimeout(0.2)
        self.d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.d.bind((host, 2222))
        self.d.settimeout(0.2)
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._disc_loop, daemon=True).start()

    def _disc_loop(self):
        while not self._stop.is_set():
            try:
                d, peer = self.d.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if d.startswith(b'WING?'):
                self.disc += 1
                self.d.sendto(f'WING,{self.host},Josephs-Wing,WING,S1234,3.1'.encode(), peer)

    def value(self, a):
        if a in self.st:
            return self.st[a]
        if a.endswith(('name', '/grp', 'tags', '$name')):
            return {'/ch/1/$name': 'Kick', '/main/1/name': 'LR'}.get(a, '')
        if a.endswith('/in'):
            return 1
        if a.endswith(('fdr', 'lvl')):
            return -10.0
        return 0

    def _loop(self):
        while not self._stop.is_set():
            try:
                d, peer = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            a, args = parse(d)
            if a == '/*S':
                continue
            if args:
                self.writes.append((a, args[0])); self.st[a] = args[0]
                continue
            v = self.value(a)
            if isinstance(v, str):
                self.s.sendto(msg(a, v), peer)
            else:
                disp = str(v)
                self.s.sendto(msg(a, disp, 0.5, float(v) if isinstance(v, float) else int(v)), peer)

    def stop(self):
        self._stop.set(); self.s.close(); self.d.close()


def main():
    fake = FakeWing()
    tmp, mixer, mx, c = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'wing', 'spotify': {'enabled': False},
                               'rtc': {'enabled': False}},
                              state={'order': ['ch/40', 'ch/2', 'aux/1'], 'order_x32': ['ch/5']})
    try:
        print('WING path')
        check('configured wing -> Wing driver', mx.console == 'wing' and type(mx.wing).__name__ == 'Wing')
        check('wing caps', mx.caps['nch'] == 40 and mx.caps['nmg'] == 8 and mx.caps['sheet'] and mx.caps['listen'])
        check('WING order loaded from "order"', mx.order == ['ch/40', 'ch/2', 'aux/1'], mx.order)
        check('connected + loaded', wait_for(lambda: mx.wing.loaded, 12))
        st = c.get('/mixer/api/state').get_json()
        check('snapshot has ch 40 and caps', '/ch/40/fdr' in st['state'] and st['caps']['console'] == 'wing')
        check('feeds present (listen on)', len(st['feeds']) == 24, len(st['feeds']))
        usb = {f['id']: f['usb'] for f in st['feeds']}
        check('v3.9 WING layout: main 43/44, main2 45/46, mon 47/48, bus1 1/2, mtx4 39/40, ambient 42',
              usb.get('main1') == [43, 44] and usb.get('main2') == [45, 46] and usb.get('mon1') == [47, 48]
              and usb.get('bus1') == [1, 2] and usb.get('bus16') == [31, 32] and usb.get('mtx4') == [39, 40]
              and usb.get('ambient') == [42, 42], usb)
        check('USB patch written', wait_for(lambda: any(a.startswith('/io/out/USB/') for a, _ in fake.writes), 4))
        check('USB patch: 43 = MAIN 1, 45/46 = MAIN 3/4, 1 = BUS 1, 41 untouched',
              wait_for(lambda: '/io/out/USB/42/grp' in dict(fake.writes), 15)
              and dict(fake.writes).get('/io/out/USB/46/in') == 4
              and dict(fake.writes).get('/io/out/USB/43/grp') == 'MAIN' and dict(fake.writes).get('/io/out/USB/43/in') == 1
              and dict(fake.writes).get('/io/out/USB/45/in') == 3 and dict(fake.writes).get('/io/out/USB/1/grp') == 'BUS'
              and '/io/out/USB/41/grp' not in dict(fake.writes), {k: v for k, v in fake.writes if '/USB/4' in k or k.endswith(('USB/1/grp', 'USB/1/in'))})
        r = c.post('/mixer/api/listen/set', json={'sub_on': True, 'sub_db': -6}).get_json()
        b = r['listen']['blend']
        check('sub blend on main1: route carries Main 2 USB 45/46 at -6 dB', r['ok'] and b['on'] and b['db'] == -6.0
              and mx.listener.blend and mx.listener.blend[:2] == (44, 45) and abs(mx.listener.blend[2] - 0.5012) < 1e-3, r)
        c.post('/mixer/api/feed', json={'id': 'bus3'})
        check('blend only on main1', mx.listener.blend is None and mx.listener.pair == (4, 5), (mx.listener.pair, mx.listener.blend))
        c.post('/mixer/api/feed', json={'id': 'main1'})
        check('back on main1: blend again', mx.listener.blend is not None and mx.listener.pair == (42, 43))
        r = c.post('/mixer/api/listen/set', json={'ambient_ch': 7}).get_json()
        check('ambient channel saved per console', r['ok'] and r['ambient_ch'] == 7
              and json.load(open(os.path.join(tmp, 'mixer_state.json'))).get('ambient', {}).get('wing') == 7)
        check('listen state keeps opus + blend', json.load(open(os.path.join(tmp, 'mixer_state.json'))).get('listen', {}).get('sub_db') == -6.0)
        n = len(fake.writes)
        r = c.post('/mixer/api/set', json={'a': '/ch/3/fdr', 'v': -6}).get_json()
        check('fader write', r['ok'] and wait_for(lambda: ('/ch/3/fdr', -6.0) in fake.writes[n:]), fake.writes[n:])
        check('fader sent as float', wait_for(lambda: any(a == '/ch/3/fdr' and isinstance(v, float) for a, v in fake.writes[n:])))
        r = c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 40}).get_json()
        check('own mute toggle on ch 40', r['ok'] and r['action'] == 'own' and wait_for(lambda: fake.st.get('/ch/40/mute') == 1), r)
        check('mgrp 8 allowed on WING', c.post('/mixer/api/set', json={'a': '/mgrp/8/mute', 'v': 0}).status_code == 200)
        html = c.get('/mixer').get_data(as_text=True)
        check('page CAPS = wing', '"console":"wing"' in html and '"nch":40' in html)
        check('node API still served (not 409)', c.get('/mixer/api/node?path=/ch/1/eq').status_code != 409)
        # v4.0.3: assignments (tags), send tap, output-strip nodes
        fake.st['/ch/5/tags'] = '#M1,#M2'
        r = c.post('/mixer/api/assign', json={'kind': 'ch', 'n': 5, 'grp': 'D', 'idx': 3, 'on': True}).get_json()
        check('assign ch5 -> DCA 3 writes tags', r['ok'] and wait_for(lambda: fake.st.get('/ch/5/tags') == '#M1,#M2,#D3'), (r, fake.st.get('/ch/5/tags')))
        r = c.post('/mixer/api/assign', json={'kind': 'ch', 'n': 5, 'grp': 'M', 'idx': 2, 'on': False}).get_json()
        check('assign ch5 out of MG 2', r['ok'] and wait_for(lambda: fake.st.get('/ch/5/tags') == '#M1,#D3'), fake.st.get('/ch/5/tags'))
        fake.st['/bus/2/tags'] = ''
        r = c.post('/mixer/api/assign', json={'kind': 'bus', 'n': 2, 'grp': 'M', 'idx': 8, 'on': True}).get_json()
        check('assign bus 2 -> MG 8', r['ok'] and wait_for(lambda: fake.st.get('/bus/2/tags') == '#M8'))
        check('DCA 17 refused', c.post('/mixer/api/assign', json={'kind': 'ch', 'n': 5, 'grp': 'D', 'idx': 17, 'on': True}).status_code == 400)
        mx.overrides['/ch/6'] = ['#M1']
        check('MG edit refused while overridden here', c.post('/mixer/api/assign', json={'kind': 'ch', 'n': 6, 'grp': 'M', 'idx': 1, 'on': True}).status_code == 409)
        mx.overrides.pop('/ch/6')
        r = c.post('/mixer/api/set', json={'a': '/ch/1/send/4/mode', 'v': 'post'}).get_json()
        check('send tap POST', r['ok'] and r['v'] == 'POST' and wait_for(lambda: fake.st.get('/ch/1/send/4/mode') == 'POST'))
        check('send tap junk refused', c.post('/mixer/api/set', json={'a': '/ch/1/send/4/mode', 'v': 'SIDE'}).status_code == 400)
        r = c.post('/mixer/api/set', json={'a': '/ch/1/flt/lcs', 'v': '18'}).get_json()
        check('low-cut slope 18', r['ok'] and wait_for(lambda: fake.st.get('/ch/1/flt/lcs') == '18'))
        check('slope 36 refused', c.post('/mixer/api/set', json={'a': '/ch/1/flt/lcs', 'v': '36'}).status_code == 400)
        check('bus / main / mtx eq + dyn node paths accepted, mtx gate refused',
              all(c.get(f'/mixer/api/node?path={p}').status_code == 200 for p in ('/bus/16/eq', '/main/4/dyn', '/mtx/8/eq'))
              and c.get('/mixer/api/node?path=/mtx/1/gate').status_code == 400 and c.get('/mixer/api/node?path=/bus/17/eq').status_code == 400)
        r = c.post('/mixer/api/set', json={'a': '/ch/2/send/MX3/lvl', 'v': -5}).get_json()
        check('ch -> matrix send write (WING)', r['ok'] and wait_for(lambda: fake.st.get('/ch/2/send/MX3/lvl') == -5.0))
        r = c.post('/mixer/api/set', json={'a': '/dca/16/fdr', 'v': 0}).get_json()
        check('DCA 16 fader write (WING)', r['ok'] and wait_for(lambda: fake.st.get('/dca/16/fdr') == 0.0))
        check('caps: 4 mains, 16 DCAs', mx.caps['nmain'] == 4 and mx.caps['ndca'] == 16)
        r = c.post('/mixer/api/order', json={'order': ['ch/39', 'ch/1']}).get_json()
        import json as _j
        with open(os.path.join(tmp, 'mixer_state.json')) as f:
            sf = _j.load(f)
        check('WING order saved, x32 order kept', sf.get('order') == ['ch/39', 'ch/1'] and sf.get('order_x32') == ['ch/5'], sf)
    finally:
        mx.wing.stop()
        fake.stop()
    print()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
