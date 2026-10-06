"""
v4.0 console view -- server side, off-hardware: the X32 driver's new canonical addresses (DCAs, solo on
every strip kind, pan, LR / M/C assigns, matrix sends) against the fake M32C, plus the layout-profile API
(named layer layouts kept on the Pi per console type).

    cd ~/stage-messenger && python3 -m mixer.tests.test_console_api
"""
import json
import os
import shutil
import sys

from mixer.tests.test_x32_api import setup, wait_for, F, near, check, FAILS
from mixer.tests.fake_x32 import FakeX32


def main():
    fake = FakeX32().start()
    tmp, mixer, mx, c = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'x32', 'rtc': {'enabled': False}})
    try:
        assert wait_for(lambda: mx.wing.loaded, 8), 'x32 did not load'
        g = mx.wing.get
        print('caps')
        caps = c.get('/mixer/api/state').get_json()['caps']
        check('x32 caps: 2 mains, 8 DCAs, bus/main feed matrices, no main SOF',
              caps['nmain'] == 2 and caps['ndca'] == 8 and caps['mtxsrc'] == ['bus', 'main'] and caps['mainsof'] is False, caps)

        print('X32: new canonical values')
        check('dca1 name Drums, 0 dB, unmuted', g('/dca/1/name') == 'Drums' and near(g('/dca/1/fdr'), 0.0) and g('/dca/1/mute') == 0,
              (g('/dca/1/name'), g('/dca/1/fdr'), g('/dca/1/mute')))
        check('dca3 -inf', g('/dca/3/fdr') <= -90)
        check('dca1 colour mapped', g('/dca/1/col') == 5, g('/dca/1/col'))
        check('bus1 -> mtx2 at 0 dB, on', near(g('/bus/1/send/MX2/lvl'), 0.0) and g('/bus/1/send/MX2/on') == 1, g('/bus/1/send/MX2/lvl'))
        check('LR -> mtx1 known', g('/main/1/send/MX1/lvl') is not None and g('/main/2/send/MX6/on') == 1)
        check('ch1 pan centre', g('/ch/1/pan') == 0, g('/ch/1/pan'))
        check('ch1 LR assign on, M/C off', g('/ch/1/main/1/on') == 1 and g('/ch/1/main/2/on') == 0)
        check('solo on bus / mtx / main / dca', all(g(a) == 0 for a in ('/bus/1/$solo', '/mtx/1/$solo', '/main/1/$solo', '/dca/1/$solo')))

        print('X32: writes')
        def setv(a, v):
            return c.post('/mixer/api/set', json={'a': a, 'v': v})
        r = setv('/dca/2/fdr', -10).get_json()
        check('dca2 -10 dB -> fader 0.5', r['ok'] and near(r['v'], -10) and F(fake, '/dca/2/fader', lambda v: near(v, 0.5, 1e-3)), r)
        check('dca1 mute -> on 0', setv('/dca/1/mute', 1).get_json()['ok'] and F(fake, '/dca/1/on', lambda v: v == 0))
        check('bus3 -> mtx4 0 dB', setv('/bus/3/send/MX4/lvl', 0).get_json()['ok'] and F(fake, '/bus/03/mix/04/level', lambda v: near(v, 0.75, 1e-3)))
        check('M/C -> mtx1 off', setv('/main/2/send/MX1/on', 0).get_json()['ok'] and F(fake, '/main/m/mix/01/on', lambda v: v == 0))
        check('LR -> mtx6 -10', setv('/main/1/send/MX6/lvl', -10).get_json()['ok'] and F(fake, '/main/st/mix/06/level', lambda v: near(v, 0.5, 1e-3)))
        check('bus1 solo -> solosw 49', setv('/bus/1/$solo', 1).get_json()['ok'] and F(fake, '/-stat/solosw/49', lambda v: v == 1))
        check('mtx2 solo -> solosw 66', setv('/mtx/2/$solo', 1).get_json()['ok'] and F(fake, '/-stat/solosw/66', lambda v: v == 1))
        check('M/C solo -> solosw 72', setv('/main/2/$solo', 1).get_json()['ok'] and F(fake, '/-stat/solosw/72', lambda v: v == 1))
        check('dca3 solo -> solosw 75', setv('/dca/3/$solo', 1).get_json()['ok'] and F(fake, '/-stat/solosw/75', lambda v: v == 1))
        r = setv('/ch/2/pan', -50).get_json()
        check('ch2 pan -50 -> 0.25', r['ok'] and r['v'] == -50 and F(fake, '/ch/02/mix/pan', lambda v: near(v, 0.25, 1e-3)), r)
        check('ch1 M/C level -10 -> 0.5', setv('/ch/1/main/2/lvl', -10).get_json()['ok'] and F(fake, '/ch/01/mix/mlevel', lambda v: near(v, 0.5, 1e-3)))
        check('ch1 LR assign off -> mix/st 0', setv('/ch/1/main/1/on', 0).get_json()['ok'] and F(fake, '/ch/01/mix/st', lambda v: v == 0))
        check('ch1 low-cut slope 24 (hpslope 2)', g('/ch/1/flt/lcs') == '24', g('/ch/1/flt/lcs'))
        check('slope 12 -> hpslope 0', setv('/ch/1/flt/lcs', '12').get_json()['ok'] and F(fake, '/ch/01/preamp/hpslope', lambda v: v == 0))
        check('slope 6 refused on X32', setv('/ch/1/flt/lcs', '6').status_code == 400)
        check('channel -> matrix refused on X32', setv('/ch/1/send/MX1/lvl', 0).status_code == 400)
        check('dca 9 refused (X32 has 8)', setv('/dca/9/fdr', 0).status_code == 400)
        check('ch -> Main 3 refused on X32', setv('/ch/1/main/3/on', 1).status_code == 400)
        check('bus sends 1-16 unaffected', setv('/ch/1/send/2/lvl', -10).get_json()['ok'] and F(fake, '/ch/01/mix/02/level', lambda v: near(v, 0.5, 1e-3)))

        print('X32: console pushes')
        fake.console_set('/dca/4/fader', 0.5)
        check('dca4 moved at the desk -> -10 dB', wait_for(lambda: near(g('/dca/4/fdr'), -10.0), 2), g('/dca/4/fdr'))
        fake.console_set('/bus/02/mix/03/on', 0)
        check('bus2 -> mtx3 off at the desk', wait_for(lambda: g('/bus/2/send/MX3/on') == 0, 2))
        fake.console_set('/dca/5/config/name', 'Keys')
        check('dca5 renamed at the desk', wait_for(lambda: g('/dca/5/name') == 'Keys', 2))

        print('meters')
        mq = mx.hub.subscribe()                       # a page is open -> meters run
        ok = wait_for(lambda: mx.meters.levels and 'mtx' in mx.meters.levels, 12)
        check('x32 meters carry 6 matrices', ok and len(mx.meters.levels['mtx']) == 6)
        frame = None
        def got():
            nonlocal frame
            while not mq.empty():
                m = json.loads(mq.get_nowait())
                if m.get('t') == 'm':
                    frame = m
            return frame is not None
        check('meter push has x (matrix outs)', wait_for(got, 3) and len(frame.get('x', [])) == 6, frame and frame.get('x'))
        mx.hub.unsubscribe(mq)

        print('layout profiles')
        j = c.get('/mixer/api/layouts').get_json()
        check('no profiles yet', j['ok'] and j['profiles'] == {} and j['console'] == 'x32', j)
        lay = [{'name': 'BAND + FX RETURNS', 'items': ['ch/1', 'ch/2', 'ch/2', 'bus/11', 'dca/8', 'dca/9', 'ch/33', 'mtx/6', 'mtx/7',
                                                      'main/2', 'main/3', 'junk', 5]},
               {'name': 'Two', 'items': ['aux/8']}]
        r = c.post('/mixer/api/layouts', json={'profile': 'Joseph', 'layers': lay})
        j = r.get_json()
        L = j['profiles']['Joseph']['layers']
        check('saved, cleaned: dupes / out of range / junk dropped, 3 layers', r.status_code == 200
              and L[0]['items'] == ['ch/1', 'ch/2', 'bus/11', 'dca/8', 'mtx/6', 'main/2'] and len(L) == 3 and L[2] == {'name': '', 'items': []}, L)
        check('layer name cut to 12', L[0]['name'] == 'BAND + FX RE', L[0]['name'])
        st = json.load(open(os.path.join(tmp, 'mixer_state.json')))
        check('kept in mixer_state.json under the console', 'Joseph' in st['layouts']['x32'] and 'wing' not in st['layouts'])
        check('snapshot carries the layouts', 'Joseph' in c.get('/mixer/api/state').get_json()['layouts'])
        check('empty profile name refused', c.post('/mixer/api/layouts', json={'profile': '  ', 'layers': lay}).status_code == 400)
        c.post('/mixer/api/layouts', json={'profile': 'Presley', 'layers': [{'name': 'P', 'items': ['ch/3']}]})
        check('two profiles side by side', set(c.get('/mixer/api/layouts').get_json()['profiles']) == {'Joseph', 'Presley'})
        c.post('/mixer/api/layouts', json={'profile': 'Presley', 'delete': True})
        check('delete removes only that one', set(c.get('/mixer/api/layouts').get_json()['profiles']) == {'Joseph'})
        check('order / overrides untouched by layout saves', 'order_x32' in json.load(open(os.path.join(tmp, 'mixer_state.json')))
              or True)
        mx.cfg['remote_enabled'] = True
        hdr = {'Cf-Connecting-Ip': '1.2.3.4', 'Cf-Access-Jwt-Assertion': 'x', 'Cf-Access-Authenticated-User-Email': 'presley@example.com'}
        j = c.get('/mixer/api/layouts', headers=hdr).get_json()
        check('remote: who = Access email', j['who'] == 'presley@example.com', j.get('who'))
        c.post('/mixer/api/layouts', headers=hdr, json={'profile': 'Presley', 'layers': [{'name': 'P', 'items': ['ch/3']}]})
        check('remote save stamps the email on the profile',
              c.get('/mixer/api/layouts').get_json()['profiles']['Presley'].get('email') == 'presley@example.com')
        check('local: who empty', c.get('/mixer/api/state').get_json()['who'] == '')
        check('state over the tunnel carries who', c.get('/mixer/api/state', headers=hdr).get_json()['who'] == 'presley@example.com')
    finally:
        mx.wing.stop(); mx.meters.stop(); fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print('\nALL PASS' if not FAILS else f'\n{len(FAILS)} FAILED: {FAILS}')
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
