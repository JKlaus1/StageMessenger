"""
Off-hardware API suite for the X32 / M32 driver (v3.0): real blueprint + Flask test client against
the stateful fake console (fake_x32.py on 127.0.0.1:10023).

    cd ~/stage-messenger && python3 -m mixer.tests.test_x32_api

Runs from a temp copy of the package, so no mixer_config.json / mixer_state.json lands in the repo.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

FAILS = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f'   ({detail})' if detail and not cond else ''), flush=True)
    if not cond:
        FAILS.append(name)


def wait_for(fn, timeout=3.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def F(fake, addr, pred, timeout=1.5):
    """wait until the fake console holds a value matching pred (writes travel over UDP)."""
    return wait_for(lambda: pred(fake.st.get(addr)), timeout)


def setup(cfg, state=None):
    tmp = tempfile.mkdtemp(prefix='x32test_')
    shutil.copytree(PKG, os.path.join(tmp, 'mixer'), ignore=shutil.ignore_patterns('tests', '__pycache__'))
    with open(os.path.join(tmp, 'mixer_config.json'), 'w') as f:
        json.dump(cfg, f)
    if state is not None:
        with open(os.path.join(tmp, 'mixer_state.json'), 'w') as f:
            json.dump(state, f)
    for m in [m for m in sys.modules if m == 'mixer' or m.startswith('mixer.')]:
        del sys.modules[m]
    sys.path.insert(0, tmp)
    import mixer
    sys.path.pop(0)
    from flask import Flask
    app = Flask('t')
    mx = mixer.init_mixer(app)
    return tmp, mixer, mx, app.test_client()


def main():
    sys.path.insert(0, os.path.dirname(PKG))
    from mixer.tests.fake_x32 import FakeX32
    sys.path.pop(0)
    fake = FakeX32().start()
    cfg = {'mixer_ip': '127.0.0.1', 'mixer_type': 'auto', 'remote_enabled': False,
           'spotify': {'enabled': True}, 'rtc': {'enabled': True}}
    tmp, mixer, mx, c = setup(cfg, state={'order': ['ch/40', 'ch/2', 'aux/1']})
    try:
        print('detect / caps')
        check('auto detect -> x32', mx.console == 'x32' and mx.x32)
        check('caps model from /xinfo', mx.caps['model'] == 'M32C' and mx.caps['fw'] == '4.06-8', mx.caps)
        check('spotify not started on x32', mx.spotify is None)
        check('driver connected + loaded', wait_for(lambda: mx.wing.loaded, 8))
        st = c.get('/mixer/api/state').get_json()
        check('snapshot caps', st['caps']['nch'] == 32 and st['caps']['nmg'] == 6 and not st['caps']['sheet'])
        check('snapshot sp None, feeds []', st['sp'] is None and st['feeds'] == [])
        s = st['state']

        print('canonical values')
        check('ch1 name', s.get('/ch/1/$name') == 'Kick' and s.get('/ch/1/name') == 'Kick')
        check('ch1 fader -7.3 dB', s.get('/ch/1/fdr') == -7.3, s.get('/ch/1/fdr'))
        check('ch2 fader -inf', s.get('/ch/2/fdr') == -144.0)
        check('ch1 colour BL -> palette 2', s.get('/ch/1/$col') == 2)
        check('ch18 colour WHi -> palette 18', s.get('/ch/18/$col') == 18)
        check('ch1 tags #M1,#M2', s.get('/ch/1/tags') == '#M1,#M2')
        check('ch17 no tags', s.get('/ch/17/tags') == '')
        check('ch21 own mute', s.get('/ch/21/mute') == 1 and s.get('/ch/21/$mute') == 1)
        check('ch1 unmuted', s.get('/ch/1/mute') == 0 and s.get('/ch/1/$mute') == 0)
        check('ch1 source AES A 1', s.get('/ch/1/in/conn/grp') == 'A' and s.get('/ch/1/in/conn/in') == 1)
        check('ch9 source AES A 9', s.get('/ch/9/in/conn/grp') == 'A' and s.get('/ch/9/in/conn/in') == 9)
        check('ch25 source AES B 17', s.get('/ch/25/in/conn/grp') == 'B' and s.get('/ch/25/in/conn/in') == 17)
        check('aux1 source Aux 1', s.get('/aux/1/in/conn/grp') == 'AUX' and s.get('/aux/1/in/conn/in') == 1)
        check('ch1 send1 -19 dB on', s.get('/ch/1/send/1/lvl') == -19.0 and s.get('/ch/1/send/1/on') == 1)
        check('main1 = /main/st muted', s.get('/main/1/mute') == 1 and s.get('/main/1/name') == 'Main Array')
        check('main2 = /main/m', s.get('/main/2/fdr') == -0.0 or s.get('/main/2/fdr') == 0.0, s.get('/main/2/fdr'))
        check('bus1 name trimmed', s.get('/bus/1/name') == 'Gtr')
        check('mgrp 1..6 present, no 7', '/mgrp/6/mute' in s and '/mgrp/7/mute' not in s)
        check('io/altsw 0', s.get('/io/altsw') == 0)
        check('no ch33 on x32', '/ch/33/fdr' not in s)

        print('writes')
        q = mx.hub.subscribe()
        r = c.post('/mixer/api/set', json={'a': '/ch/1/fdr', 'v': -10}).get_json()
        check('set fader -> dB echo', r == {'ok': True, 'v': -10.0}, r)
        check('fake got 0.5', F(fake, '/ch/01/mix/fader', lambda v: abs(v - 0.5) < 1e-6), fake.st['/ch/01/mix/fader'])
        r = c.post('/mixer/api/set', json={'a': '/ch/1/fdr', 'v': 4.5}).get_json()
        check('fader +4.5 -> 0.8625', F(fake, '/ch/01/mix/fader', lambda v: abs(v - 0.8625) < 1e-6) and r['v'] == 4.5)
        r = c.post('/mixer/api/set', json={'a': '/ch/3/send/2/lvl', 'v': -20}).get_json()
        check('send level', F(fake, '/ch/03/mix/02/level', lambda v: abs(v - 0.375) < 1e-6) and r['v'] == -20.0)
        c.post('/mixer/api/set', json={'a': '/aux/2/send/16/on', 'v': 0})
        check('aux send on -> /auxin/02/mix/16/on', F(fake, '/auxin/02/mix/16/on', lambda v: v == 0))
        c.post('/mixer/api/set', json={'a': '/bus/4/mute', 'v': 1})
        check('bus mute -> mix/on 0', F(fake, '/bus/04/mix/on', lambda v: v == 0) and mx.wing.get('/bus/4/mute') == 1)
        c.post('/mixer/api/set', json={'a': '/main/2/fdr', 'v': -5})
        check('main2 fader -> /main/m', F(fake, '/main/m/mix/fader', lambda v: abs(v - 0.625) < 1e-6))
        c.post('/mixer/api/set', json={'a': '/mtx/6/fdr', 'v': 0})
        check('mtx6 fader', F(fake, '/mtx/06/mix/fader', lambda v: abs(v - 0.75) < 1e-6))
        c.post('/mixer/api/set', json={'a': '/ch/5/$solo', 'v': 1})
        check('solo ch5 -> solosw/05', F(fake, '/-stat/solosw/05', lambda v: v == 1))
        c.post('/mixer/api/set', json={'a': '/aux/3/$solo', 'v': 1})
        check('solo aux3 -> solosw/35', F(fake, '/-stat/solosw/35', lambda v: v == 1))
        c.post('/mixer/api/set', json={'a': '/ch/5/$solo', 'v': 0}); c.post('/mixer/api/set', json={'a': '/aux/3/$solo', 'v': 0})
        bad = [c.post('/mixer/api/set', json={'a': a, 'v': 1}).status_code for a in
               ('/mgrp/7/mute', '/ch/33/fdr', '/aux/9/mute', '/mtx/7/fdr', '/main/3/fdr', '/ch/1/send/17/lvl',
                '/io/in/A/1/g', '/ch/1/in/set/trim', '/cards/wlive/auto_play')]
        check('out-of-range / unsupported writes -> 400', all(x == 400 for x in bad), bad)

        print('mute groups (M32 semantics)')
        r = c.post('/mixer/api/set', json={'a': '/mgrp/1/mute', 'v': 1}).get_json()
        check('engage MG1', r['ok'] and F(fake, '/config/mute/1', lambda v: v == 1))
        check('members group-muted ($mute 2, own 0)',
              wait_for(lambda: mx.wing.get('/ch/1/$mute') == 2 and mx.wing.get('/ch/2/$mute') == 2 and mx.wing.get('/ch/1/mute') == 0),
              (mx.wing.get('/ch/1/$mute'), mx.wing.get('/ch/1/mute')))
        check('non-member ch17 untouched', mx.wing.get('/ch/17/$mute') == 0)
        check('ch21 (own-muted member) shows group-muted', mx.wing.get('/ch/21/$mute') == 2)
        r = c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 1}).get_json()
        check('override ch1 -> action override, $mute 0', r == {'ok': True, 'action': 'override', 'state': 0}, r)
        check('fake ch1 mix/on 1, MG1 still on', F(fake, '/ch/01/mix/on', lambda v: v == 1) and fake.st['/config/mute/1'] == 1)
        check('ch2 still group-muted', mx.wing.get('/ch/2/$mute') == 2)
        r = c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 1}).get_json()
        check('second press re-mutes (group-muted again)', r['state'] == 2 and F(fake, '/ch/01/mix/on', lambda v: v == 0), r)
        c.post('/mixer/api/set', json={'a': '/mgrp/1/mute', 'v': 0})
        check('release MG1 -> members unmuted (console unmutes all)',
              wait_for(lambda: mx.wing.get('/ch/1/$mute') == 0 and mx.wing.get('/ch/21/$mute') == 0))
        r = c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 17}).get_json()
        check('own mute ch17', r['action'] == 'own' and r['state'] == 1 and F(fake, '/ch/17/mix/on', lambda v: v == 0), r)
        c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 17})
        check('own unmute ch17', F(fake, '/ch/17/mix/on', lambda v: v == 1))
        check('mute bad strip -> 400', c.post('/mixer/api/mute', json={'kind': 'ch', 'n': 33}).status_code == 400)

        print('console pushes -> canonical events')
        while not q.empty():
            q.get_nowait()
        fake.console_set('/ch/07/mix/fader', 0.25)
        fake.console_set('/ch/07/config/source', 33)
        fake.console_set('/config/routing/IN/9-16', 16)
        fake.console_set('/config/routing/routswitch', 1)
        fake.console_set('/ch/03/config/name', 'pad ')
        fake.console_set('/ch/03/config/color', 9)
        got = {}
        def drain():
            while not q.empty():
                m = json.loads(q.get_nowait())
                if m.get('t') == 'upd':
                    got[m['a']] = m['v']
            return '/io/altsw' in got and '/ch/3/$col' in got
        wait_for(drain, 3)
        check('fader push -> /ch/7/fdr -30.0', got.get('/ch/7/fdr') == -30.0, got.get('/ch/7/fdr'))
        check('re-patch push -> /ch/7/in/conn AUX 1', got.get('/ch/7/in/conn/grp') == 'AUX' and got.get('/ch/7/in/conn/in') == 1)
        check('IN block push -> ch9 Card 1', got.get('/ch/9/in/conn/grp') == 'CRD' and got.get('/ch/9/in/conn/in') == 1, (got.get('/ch/9/in/conn/grp'), got.get('/ch/9/in/conn/in')))
        check('routswitch push -> /io/altsw 1 + altsrc', got.get('/io/altsw') == 1 and got.get('/ch/1/in/set/altsrc') == 1)
        check('PLAY block -> alt source Local 1', mx.wing.get('/ch/1/in/conn/altgrp') == 'LCL' and mx.wing.get('/ch/1/in/conn/altin') == 1)
        check('name push trimmed', got.get('/ch/3/$name') == 'pad')
        check('colour 9 (RDi) -> palette 9', got.get('/ch/3/$col') == 9)
        fake.console_set('/config/routing/routswitch', 0)
        fake.console_set('/config/userrout/in/01', 129)
        fake.console_set('/config/routing/IN/1-8', 20)
        check('user-in block -> Card 1', wait_for(lambda: mx.wing.get('/ch/1/in/conn/grp') == 'CRD' and mx.wing.get('/ch/1/in/conn/in') == 1))
        fake.console_set('/config/routing/IN/1-8', 4)

        print('meters')
        mq = mx.hub.subscribe()
        fake.meter_level = {0: 0.1, 48: 0.1, 32: 0.01}
        ok = wait_for(lambda: mx.meters.levels is not None, 12)
        check('meter subscription while a page is open', ok)
        m = None
        def mframe():
            nonlocal m
            while not mq.empty():
                x = json.loads(mq.get_nowait())
                if x.get('t') == 'm':
                    m = x
            return m is not None
        wait_for(mframe, 3)
        check('meter frame shape 32/8/16/2', m and len(m['c']) == 32 and len(m['a']) == 8 and len(m['b']) == 16 and len(m['m']) == 2,
              m and (len(m['c']), len(m['a']), len(m['b']), len(m['m'])))
        if m:
            f1 = mx.wing.get('/ch/1/fdr')
            check('ch1 pre -20, post = pre + fader', m['c'][0][0] == -20 and m['c'][0][1] == round(-20 + f1), (m['c'][0], f1))
            check('ch2 fader -inf -> post -99', m['c'][1][1] == -99)
            check('aux1 muted -> post -99, pre -40', m['a'][0] == [-40, -99], m['a'][0])
            check('bus1 out = pre + 0 dB', m['b'][0] == -20, m['b'][0])
        mx.hub.unsubscribe(mq)

        print('x32-off features')
        for path, meth in (('/mixer/api/node?path=/ch/1/eq', 'get'), ('/mixer/api/srcnames?g=A', 'get'),
                           ('/mixer/stream.mp3', 'get'), ('/mixer/api/repatch', 'post')):
            code = getattr(c, meth)(path).status_code
            check(f'{path} -> 409', code == 409, code)
        for path, body in (('/mixer/api/patch', {'kind': 'ch', 'n': 1, 'grp': 'A', 'in': 1}),
                           ('/mixer/api/nodeset', {'path': '/ch/1/eq', 'key': 'on', 'value': 1}),
                           ('/mixer/api/rec', {'action': 'rec', 'card': 1}),
                           ('/mixer/api/play', {'action': 'play', 'card': 1})):
            code = c.post(path, json=body).status_code
            check(f'{path} -> 409', code == 409, code)
        check('whep -> 404 on x32', c.post('/mixer/api/rtc/whep', data='v=0').status_code == 404)

        print('page')
        html = c.get('/mixer').get_data(as_text=True)
        check('CAPS injected', '"console":"x32"' in html and 'const CAPS = null' not in html)
        check('page not cached', c.get('/mixer').headers.get('Cache-Control') == 'no-store')

        print('order (per console)')
        check('x32 order starts empty (WING order is its own)', mx.order == [], mx.order)
        r = c.post('/mixer/api/order', json={'order': ['ch/3', 'ch/33', 'aux/8']}).get_json()
        check('save order keeps valid keys', r['order'] == ['ch/3', 'aux/8'], r)
        with open(os.path.join(tmp, 'mixer_state.json')) as f:
            sf = json.load(f)
        check('WING order untouched, x32 order separate, console remembered',
              sf.get('order') == ['ch/40', 'ch/2', 'aux/1'] and sf.get('order_x32') == ['ch/3', 'aux/8'] and sf.get('console') == 'x32', sf)

        print('reconnect')
        n_before = len(fake.writes)
        check('no writes during load / idle', all(not a.startswith('/config/routing') for a, _ in fake.writes[:n_before]))
    finally:
        mx.wing.stop(); mx.meters.stop()
        fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
