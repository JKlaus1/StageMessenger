"""
WING path regression (v3.0): with mixer_type 'wing' the controller must behave exactly as before the
X32 work -- WING driver, 40/8/16 strips, USB patch ownership, own-mute toggle, float fader writes,
the WING channel order key. Minimal fake WING on 127.0.0.1:2223 (query reply [display, norm, native]).

    cd ~/stage-messenger && python3 -m mixer.tests.test_wing_regression
"""
import os
import socket
import sys
import threading
import time

from mixer.tests.test_x32_api import check, wait_for, setup, FAILS
from mixer.tests.fake_x32 import msg, parse


class FakeWing:
    def __init__(self):
        self.st = {}
        self.writes = []
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(('127.0.0.1', 2223))
        self.s.settimeout(0.2)
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

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
        self._stop.set(); self.s.close()


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
        check('feeds present (listen on)', len(st['feeds']) == 23, len(st['feeds']))
        check('USB patch written', wait_for(lambda: any(a.startswith('/io/out/USB/') for a, _ in fake.writes), 4))
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
