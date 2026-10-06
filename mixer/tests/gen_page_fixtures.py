"""
Fixtures for test_page.js: the page exactly as the blueprint serves it (CAPS injected) plus a live
snapshot, for the X32 driver (fake M32C, mute group 1 engaged at the 'console') and the WING default.

    cd ~/stage-messenger && python3 -m mixer.tests.gen_page_fixtures /tmp/fx && node mixer/tests/test_page.js /tmp/fx
"""
import json
import os
import sys

from mixer.tests.test_x32_api import setup, wait_for
from mixer.tests.fake_x32 import FakeX32


def main(out):
    os.makedirs(out, exist_ok=True)
    fake = FakeX32().start()
    tmp, mixer, mx, c = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'x32', 'rtc': {'enabled': False}})
    try:
        assert wait_for(lambda: mx.wing.loaded, 8), 'x32 did not load'
        fake.console_set('/config/mute/1', 1)
        assert wait_for(lambda: mx.wing.get('/ch/2/$mute') == 2, 3), 'group mute not seen'
        open(os.path.join(out, 'x32.html'), 'w').write(c.get('/mixer').get_data(as_text=True))
        json.dump(c.get('/mixer/api/state').get_json(), open(os.path.join(out, 'x32.json'), 'w'))
        api = {}                                       # real driver answers for the page's fetch mock
        for path in ('/ch/1/eq', '/ch/1/gate', '/ch/1/dyn', '/aux/1/eq', '/bus/5/eq', '/bus/5/dyn'):
            api['/mixer/api/node?path=' + path.replace('/', '%2F')] = c.get('/mixer/api/node?path=' + path).get_json()
        for g in ('IN', 'AUX', 'USB', 'FX', 'BUS'):
            api['/mixer/api/srcnames?g=' + g] = c.get('/mixer/api/srcnames?g=' + g).get_json()
        json.dump(api, open(os.path.join(out, 'x32_api.json'), 'w'))
    finally:
        mx.wing.stop(); mx.meters.stop(); fake.stop()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    open(os.path.join(out, 'wing.html'), 'w').write(open(os.path.join(here, 'mixer.html')).read())
    print('fixtures in', out)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/mixer_fx')
