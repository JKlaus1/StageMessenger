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


def near(a, b, tol=0.02):
    return isinstance(a, (int, float)) and abs(a - b) <= tol


def sheet_suite(c, mx, fake):
    """v3.1 channel sheet: input stage, headamps, EQ / gate / dyn, source picker."""
    print('sheet: input stage')
    g = mx.wing.get
    check('ch1 trim +15', near(g('/ch/1/in/set/trim'), 15.0), g('/ch/1/in/set/trim'))
    check('ch1 low cut on, 46 Hz', g('/ch/1/flt/lc') == 1 and g('/ch/1/flt/lcf') == 46, g('/ch/1/flt/lcf'))
    check('aux1 trim +2, no low cut', near(g('/aux/1/in/set/trim'), 2.0) and g('/aux/1/flt/lc') is None)
    check('no stagebox -> no preamp values', g('/io/in/A/1/g') is None and g('/io/in/A/1/vph') is None)
    r = c.post('/mixer/api/set', json={'a': '/ch/1/in/set/trim', 'v': 6}).get_json()
    check('trim +6 -> 0.6667', r['v'] == 6.0 and F(fake, '/ch/01/preamp/trim', lambda v: near(v, 24 / 36, 1e-4)))
    r = c.post('/mixer/api/set', json={'a': '/ch/1/in/set/trim', 'v': -30}).get_json()
    check('trim clamps to -18', r['v'] == -18.0 and F(fake, '/ch/01/preamp/trim', lambda v: near(v, 0, 1e-6)))
    c.post('/mixer/api/set', json={'a': '/ch/1/in/set/inv', 'v': 1})
    check('polarity', F(fake, '/ch/01/preamp/invert', lambda v: v == 1) and g('/ch/1/in/set/inv') == 1)
    r = c.post('/mixer/api/set', json={'a': '/ch/1/flt/lcf', 'v': 100}).get_json()
    check('low cut 100 Hz -> ln5/ln20', r['v'] == 100 and F(fake, '/ch/01/preamp/hpf', lambda v: near(v, 0.53724, 1e-4)), r)
    r = c.post('/mixer/api/set', json={'a': '/ch/1/flt/lcf', 'v': 2000}).get_json()
    check('low cut clamps to 400', r['v'] == 400 and F(fake, '/ch/01/preamp/hpf', lambda v: near(v, 1.0, 1e-6)), r)
    c.post('/mixer/api/set', json={'a': '/ch/1/flt/lc', 'v': 0})
    check('low cut off', F(fake, '/ch/01/preamp/hpon', lambda v: v == 0))
    c.post('/mixer/api/set', json={'a': '/aux/2/in/set/trim', 'v': -6})
    check('aux trim -> /auxin/02/preamp/trim', F(fake, '/auxin/02/preamp/trim', lambda v: near(v, 12 / 36, 1e-4)))

    print('sheet: headamps (stagebox appears / goes)')
    fake.console_set('/-ha/00/index', 32)                     # ch 1 -> AES50 A 1 now has a preamp
    check('ha index -> /io/in/A/1 gain +15.5, 48V on',
          wait_for(lambda: g('/io/in/A/1/g') == 15.5 and g('/io/in/A/1/vph') == 1), (g('/io/in/A/1/g'), g('/io/in/A/1/vph')))
    check('other headamps stay hidden', g('/io/in/A/2/g') is None and g('/io/in/B/1/g') is None)
    r = c.post('/mixer/api/set', json={'a': '/io/in/A/1/g', 'v': 20}).get_json()
    check('gain +20 -> 32/72', r['v'] == 20.0 and F(fake, '/headamp/032/gain', lambda v: near(v, 32 / 72, 1e-4)), r)
    r = c.post('/mixer/api/set', json={'a': '/io/in/A/1/g', 'v': 61.3}).get_json()
    check('gain clamps to +60', r['v'] == 60.0 and F(fake, '/headamp/032/gain', lambda v: near(v, 1.0, 1e-6)), r)
    c.post('/mixer/api/set', json={'a': '/io/in/A/1/vph', 'v': 0})
    check('48V off', F(fake, '/headamp/032/phantom', lambda v: v == 0))
    check('gain on a headamp with no preamp -> 400', c.post('/mixer/api/set', json={'a': '/io/in/B/5/g', 'v': 10}).status_code == 400)
    fake.console_set('/-ha/00/index', -1)
    check('stagebox gone -> preamp hidden again', wait_for(lambda: g('/io/in/A/1/g') is None))

    print('sheet: processing read (probed Ch 1 values)')
    def node(path):
        r = c.get('/mixer/api/node?path=' + path)
        return r.status_code, {p['key']: p for p in (r.get_json().get('params') or [])}
    code, eq = node('/ch/1/eq')
    check('eq: 17 params', code == 200 and len(eq) == 17, (code, len(eq)))
    check('eq band 1 PEQ 58.3 Hz +3.0 dB Q~2.0', eq.get('1type', {}).get('value') == 'PEQ' and near(eq['1f']['value'], 58.3, 0.1)
          and near(eq['1g']['value'], 3.0) and near(eq['1q']['value'], 2.0, 0.06), {k: eq[k]['value'] for k in ('1f', '1g', '1q')})
    check('eq band 2 497 Hz -10.75 dB Q 0.6', near(eq['2f']['value'], 496.6, 1) and near(eq['2g']['value'], -10.75, 0.01)
          and near(eq['2q']['value'], 0.6, 0.01), {k: eq[k]['value'] for k in ('2f', '2g', '2q')})
    check('eq band 3 Q 8.2, band 4 VEQ 4.68 kHz +5.25', near(eq['3q']['value'], 8.2, 0.05) and eq['4type']['value'] == 'VEQ'
          and near(eq['4f']['value'], 4680, 10) and near(eq['4g']['value'], 5.25, 0.01))
    check('eq q param is log 0.3..10', eq['1q']['type'] == 'log' and eq['1q']['lo'] == 0.3 and eq['1q']['hi'] == 10)
    code, gt = node('/ch/1/gate')
    check('gate GATE -30 dB range 60, hold 70.9 ms, release 151 ms', code == 200 and gt['mode']['value'] == 'GATE'
          and gt['thr']['value'] == -30 and gt['range']['value'] == 60 and near(gt['hld']['value'], 70.9, 0.2)
          and near(gt['rel']['value'], 151, 1), {k: v['value'] for k, v in gt.items()})
    check('gate key filter 1.0 @ 60.4 Hz', gt['ftype']['value'] == '1.0' and near(gt['ff']['value'], 60.4, 0.2) and gt['fon']['value'] == 0)
    code, dy = node('/ch/1/dyn')
    check('dyn COMP PEAK LIN -21 dB 1.5:1 knee 1, +6.5 makeup', dy['mode']['value'] == 'COMP' and dy['det']['value'] == 'PEAK'
          and dy['env']['value'] == 'LIN' and dy['thr']['value'] == -21 and dy['ratio']['value'] == '1.5'
          and dy['knee']['value'] == 1 and dy['gain']['value'] == 6.5, {k: v['value'] for k, v in dy.items()})
    check('dyn attack 6 hold 56.3 release 185 POST mix 100', dy['att']['value'] == 6 and near(dy['hld']['value'], 56.3, 0.2)
          and near(dy['rel']['value'], 185, 1) and dy['pos']['value'] == 'POST' and dy['mix']['value'] == 100)
    code, aq = node('/aux/1/eq')
    check('aux eq served', code == 200 and len(aq) == 17)
    check('aux gate / dyn not on the M32 -> 400', node('/aux/1/gate')[0] == 400 and node('/aux/1/dyn')[0] == 400)
    check('ch 33 / bus node -> 400', node('/ch/33/eq')[0] == 400 and node('/bus/1/eq')[0] == 400)

    print('sheet: processing writes')
    def ns(path, key, value):
        r = c.post('/mixer/api/nodeset', json={'path': path, 'key': key, 'value': value})
        return r.status_code, r.get_json()
    code, r = ns('/ch/1/eq', '2g', -6)
    check('eq 2 gain -6 -> 0.3', r.get('value') == -6.0 and F(fake, '/ch/01/eq/2/g', lambda v: near(v, 0.3, 1e-6)), r)
    code, r = ns('/ch/1/eq', '3f', 1000)
    check('eq 3 freq 1 kHz -> log', r.get('value') == 1000 and F(fake, '/ch/01/eq/3/f', lambda v: near(v, 0.566323, 1e-5)), r)
    code, r = ns('/ch/1/eq', '1q', 0.7)
    check('eq 1 Q 0.7 -> ln(.07)/ln(.03)', near(r.get('value'), 0.7, 1e-6) and F(fake, '/ch/01/eq/1/q', lambda v: near(v, 0.758368, 1e-5)), r)
    code, r = ns('/ch/1/eq', '1q', 50)
    check('eq Q clamps to 10', near(r.get('value'), 10) and F(fake, '/ch/01/eq/1/q', lambda v: near(v, 0, 1e-6)), r)
    code, r = ns('/ch/1/eq', '1type', 'HShv')
    check('eq type HShv -> 4', r.get('value') == 'HShv' and F(fake, '/ch/01/eq/1/type', lambda v: v == 4))
    code, r = ns('/ch/1/eq', 'on', 0)
    check('eq off', r.get('value') == 0 and F(fake, '/ch/01/eq/on', lambda v: v == 0))
    code, r = ns('/ch/1/gate', 'thr', -45.5)
    check('gate thr -45.5 -> 0.43125', r.get('value') == -45.5 and F(fake, '/ch/01/gate/thr', lambda v: near(v, 0.43125, 1e-6)), r)
    code, r = ns('/ch/1/gate', 'hld', 100)
    check('gate hold 100 ms -> log', near(r.get('value'), 100, 0.5) and F(fake, '/ch/01/gate/hold', lambda v: near(v, 0.739794, 1e-5)), r)
    code, r = ns('/ch/1/gate', 'mode', 'DUCK')
    check('gate mode DUCK -> 4', F(fake, '/ch/01/gate/mode', lambda v: v == 4))
    code, r = ns('/ch/1/dyn', 'ratio', '4.0')
    check('dyn ratio 4.0 -> 6', r.get('value') == '4.0' and F(fake, '/ch/01/dyn/ratio', lambda v: v == 6))
    code, r = ns('/ch/1/dyn', 'gain', 12)
    check('dyn makeup +12 -> 0.5', r.get('value') == 12 and F(fake, '/ch/01/dyn/mgain', lambda v: near(v, 0.5, 1e-6)))
    code, r = ns('/ch/1/dyn', 'auto', 1)
    check('dyn auto on', F(fake, '/ch/01/dyn/auto', lambda v: v == 1))
    code, r = ns('/ch/1/dyn', 'ff', 2000)
    check('dyn key filter 2 kHz', F(fake, '/ch/01/dyn/filter/f', lambda v: near(v, 0.666667, 1e-5)))
    bad = [ns('/ch/1/dyn', 'ratio', '3:1')[0], ns('/ch/1/eq', 'mdl', 'STD')[0], ns('/aux/1/gate', 'thr', -20)[0],
           ns('/ch/1/gate', 'keysrc', 3)[0]]
    check('bad option / key / block -> 400', all(x == 400 for x in bad), bad)
    code, gt = node('/ch/1/gate')
    check('read-back after writes', gt['thr']['value'] == -45.5 and gt['mode']['value'] == 'DUCK')

    print('sheet: source picker')
    j = c.get('/mixer/api/srcnames?g=IN').get_json()
    names = {x['n']: x['name'] for x in j['inputs']}
    check('IN list: 32, In 1 = AES A 1, In 25 = AES B 17', len(names) == 32 and names[1] == 'AES A 1' and names[25] == 'AES B 17', names.get(25))
    check('BUS names', c.get('/mixer/api/srcnames?g=BUS').get_json()['inputs'][0]['name'] == 'Gtr')
    check('FX names', c.get('/mixer/api/srcnames?g=FX').get_json()['inputs'][1]['name'] == 'FX 1R')
    check('WING group -> 400', c.get('/mixer/api/srcnames?g=A').status_code == 400)
    r = c.post('/mixer/api/patch', json={'kind': 'ch', 'n': 2, 'grp': 'AUX', 'in': 3}).get_json()
    check('patch ch2 -> Aux 3 (source 35)', r['ok'] and F(fake, '/ch/02/config/source', lambda v: v == 35))
    check('pick + source tag follow', wait_for(lambda: g('/ch/2/in/conn/pick') == 'AUX:3' and g('/ch/2/in/conn/grp') == 'AUX'
                                               and g('/ch/2/in/conn/in') == 3))
    r = c.post('/mixer/api/patch', json={'kind': 'aux', 'n': 1, 'grp': 'BUS', 'in': 16}).get_json()
    check('patch aux1 -> Bus 16 (64)', r['ok'] and F(fake, '/auxin/01/config/source', lambda v: v == 64))
    c.post('/mixer/api/patch', json={'kind': 'ch', 'n': 2, 'grp': 'IN', 'in': 2})
    check('patch back to In 2', F(fake, '/ch/02/config/source', lambda v: v == 2) and wait_for(lambda: g('/ch/2/in/conn/pick') == 'IN:2'))
    bad = [c.post('/mixer/api/patch', json={'kind': 'ch', 'n': 1, 'grp': g_, 'in': i_}).status_code
           for g_, i_ in (('A', 1), ('IN', 33), ('USB', 3), ('BUS', 0))]
    check('bad patch -> 400', all(x == 400 for x in bad), bad)


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
        check('snapshot caps', st['caps']['nch'] == 32 and st['caps']['nmg'] == 6 and st['caps']['sheet']
              and st['caps']['gain'] == [-12.0, 60.0] and st['caps']['lcf'] == [20.0, 400.0] and not st['caps']['hc'])
        check('snapshot srcgroups = picker groups', [g for g, _ in st['srcgroups']] == ['IN', 'AUX', 'USB', 'FX', 'BUS'])
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
                '/io/in/A/1/g', '/io/in/A/1/pol', '/ch/1/flt/hc', '/ch/1/flt/hcf', '/aux/1/flt/lc', '/cards/wlive/auto_play')]
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
            check('ch dyn rows carry GR dB (6 values)', len(m['cd'][0]) == 6 and m['cd'][0][4] == 0.0, m['cd'][0])
        mx.hub.unsubscribe(mq)

        sheet_suite(c, mx, fake)

        print('x32-off features')
        for path, meth in (('/mixer/stream.mp3', 'get'), ('/mixer/api/repatch', 'post')):
            code = getattr(c, meth)(path).status_code
            check(f'{path} -> 409', code == 409, code)
        for path, body in (('/mixer/api/rec', {'action': 'rec', 'card': 1}),
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
