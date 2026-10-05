// jsdom page suite (v3.0): the served page + a live snapshot -> DOM, clicks -> requests.
//   cd ~/stage-messenger && python3 -m mixer.tests.gen_page_fixtures /tmp/fx && node mixer/tests/test_page.js /tmp/fx
// needs jsdom:  npm i jsdom   (anywhere on NODE_PATH)
const fs = require('fs'), path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');
const FX = process.argv[2] || '/tmp/mixer_fx';
let fails = 0;
const check = (name, cond, detail) => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${!cond && detail !== undefined ? '   (' + JSON.stringify(detail) + ')' : ''}`);
  if (!cond) fails++;
};
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

function load(html, snap) {
  const posts = [], errors = [];
  let es = null;
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => { if (!/Not implemented: (navigation|HTMLMediaElement)/.test(e.message)) errors.push(e.message); });
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://pi.local/mixer', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (url, opts = {}) => {
        let body = null; try { body = opts.body ? JSON.parse(opts.body) : null; } catch (e) {}
        posts.push({ url, body });
        const j = url.includes('/api/state') ? snap : url.includes('/api/node') ? { ok: false, path: '', params: [] }
          : { ok: true, action: 'override', state: 0 };
        return { ok: true, status: 200, type: 'basic', headers: { get: () => null }, json: async () => j, clone() { return this; } };
      };
      w.EventSource = class { constructor() { es = this; this.readyState = 1;
        setTimeout(() => { this.onopen && this.onopen(); this.onmessage({ data: JSON.stringify({ t: 'snap', ...snap }) }); }, 0); }
        close() {} };
      w.EventSource.OPEN = 1; w.EventSource.CLOSED = 2;
      w.alert = () => {}; w.confirm = () => true;
      w.HTMLMediaElement.prototype.play = () => Promise.resolve(); w.HTMLMediaElement.prototype.pause = () => {};
      w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
    } });
  return { dom, w: dom.window, d: dom.window.document, posts, errors, push: m => es.onmessage({ data: JSON.stringify(m) }) };
}
const rowOf = (d, key) => d.querySelector(`#strips .strip[data-key="${key}"]`);

(async () => {
  // ── X32 ──
  console.log('X32 page (M32C, mute group 1 engaged)');
  const snap = JSON.parse(fs.readFileSync(path.join(FX, 'x32.json')));
  const P = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), snap);
  await tick(80);
  const { d } = P;
  check('title from caps', d.getElementById('title').textContent === 'M32C Mixer', d.getElementById('title').textContent);
  check('40 channel strips (32 ch + 8 aux)', d.querySelectorAll('#strips .strip').length === 40, d.querySelectorAll('#strips .strip').length);
  check('16 bus strips', d.querySelectorAll('#bus-strips .strip').length === 16);
  check('6 mute group buttons', d.querySelectorAll('.mgbtn').length === 6);
  check('MG 1 lit', d.querySelector('.mgbtn[data-g="1"]').classList.contains('on'));
  check('listen card hidden', d.getElementById('listen-card').classList.contains('none'));
  check('USB re-patch footer hidden', d.getElementById('patch-foot').classList.contains('none'));
  check('status shows model', d.getElementById('status-text').textContent === 'M32C', d.getElementById('status-text').textContent);
  const r1 = rowOf(d, 'ch/1');
  check('ch1 name', r1.querySelector('.nm b').textContent === 'Kick');
  check('ch1 source tag', r1.querySelector('.nm i').textContent.includes('CH 1 · AES A 1'), r1.querySelector('.nm i').textContent);
  check('ch1 colour stripe', r1.style.borderLeftColor !== '' && r1.style.borderLeftColor !== 'transparent', r1.style.borderLeftColor);
  check('ch1 fader readout', r1.querySelector('.db').textContent === '-7.3', r1.querySelector('.db').textContent);
  const r2 = rowOf(d, 'ch/2');
  check('ch2 shows MG (group-muted)', r2.querySelector('.mute').textContent === 'MG' && r2.querySelector('.mute').classList.contains('mg'));
  const r17 = rowOf(d, 'ch/17');
  check('ch17 not in group: MUTE off', r17.querySelector('.mute').textContent === 'MUTE' && !r17.querySelector('.mute').classList.contains('on'));
  const main2 = d.querySelectorAll('#master-card .strip')[1];
  check('Main 2 labelled M/C', main2.querySelector('.nm i').textContent.includes('M/C'), main2.querySelector('.nm i').textContent);
  check('aux row present (AUX 8)', !!rowOf(d, 'aux/8') && !rowOf(d, 'ch/33'));

  P.posts.length = 0;
  r1.querySelector('.mute').click(); await tick();
  const mp = P.posts.find(p => p.url === '/mixer/api/mute');
  check('override click posts /api/mute ch 1', mp && mp.body.kind === 'ch' && mp.body.n === 1, P.posts);
  check('no page-side OVR on X32 (console override display)', !r1.querySelector('.nm i').title.includes('pulled out'), r1.querySelector('.nm i').title);
  check('ch1 button reads MUTE (unmuted) after override', r1.querySelector('.mute').textContent === 'MUTE' && !r1.querySelector('.mute').classList.contains('on'));
  P.push({ t: 'upd', a: '/ch/1/$mute', v: 0 }); await tick();
  check('ch1 marked OVR', r1.querySelector('.nm i').textContent.startsWith('OVR'), r1.querySelector('.nm i').textContent);

  P.posts.length = 0;
  rowOf(d, 'ch/5').querySelector('.solo').click(); await tick();
  const sp = P.posts.find(p => p.url === '/mixer/api/set');
  check('solo click posts $solo', sp && sp.body.a === '/ch/5/$solo' && sp.body.v === 1, P.posts);
  check('SOLO ✕ shown', d.getElementById('solo-clear').classList.contains('show'));
  r1.querySelector('.nm').click(); await tick();
  check('name tap does not open the sheet on X32', !d.getElementById('sheet').classList.contains('open'));

  P.push({ t: 'upd', a: '/ch/3/$name', v: 'NewName' }); await tick();
  check('name push repaints', rowOf(d, 'ch/3').querySelector('.nm b').textContent === 'NewName');
  P.push({ t: 'upd', a: '/ch/1/in/conn/altgrp', v: 'LCL' }); P.push({ t: 'upd', a: '/ch/1/in/conn/altin', v: 1 });
  P.push({ t: 'upd', a: '/ch/1/in/set/altsrc', v: 1 }); P.push({ t: 'upd', a: '/io/altsw', v: 1 }); await tick();
  check('playback routing tag', r1.querySelector('.nm i').textContent.includes('PLAY · Local 1'), r1.querySelector('.nm i').textContent);
  check('badge INPUTS ON PLAYBACK', d.getElementById('alt-badge').classList.contains('show') && d.getElementById('alt-badge').textContent === 'INPUTS ON PLAYBACK');
  const c = Array.from({ length: 32 }, (_, i) => [-20 - i, -99]), a = Array.from({ length: 8 }, () => [-60, -99]);
  P.push({ t: 'm', c, a, cd: c.map(() => [0, 0, 0, 0]), ad: a.map(() => [0, 0, 0, 0]), b: Array(16).fill(-30), m: [-99, -99] }); await tick();
  check('meter frame painted', !r1.querySelector('.lvl').classList.contains('off'));
  P.posts.length = 0;
  d.querySelector('.mgbtn[data-g="6"]').click(); await tick();
  const mg = P.posts.find(p => p.url === '/mixer/api/set');
  check('MG 6 posts /mgrp/6/mute', mg && mg.body.a === '/mgrp/6/mute' && mg.body.v === 1, P.posts);
  check('no script errors (X32)', P.errors.length === 0, P.errors);

  // caps mismatch -> one reload attempt (guarded)
  const P3 = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), { ...snap, caps: { ...snap.caps, console: 'wing', nch: 40 } });
  await tick(80);
  check('console change in snapshot -> reload guard set', P3.w.sessionStorage.getItem('mixer.capsReload') === '1');

  // ── WING (no CAPS injected = defaults) ──
  console.log('WING page (defaults)');
  const wsnap = { conn: true, loaded: true, state: { '/ch/1/$name': 'Kick', '/ch/1/fdr': -10, '/ch/1/in/conn/grp': 'LCL', '/ch/1/in/conn/in': 3,
                  '/mgrp/1/name': 'Drums', '/ch/40/fdr': 0 }, feeds: [{ id: 'main1', label: 'Main LR', usb: [1, 2] }], feed: 'main1',
                  listen: {}, patch: 'USB patch OK', nbus: 16, meters: false, srcgroups: [], ovr: [], order: [], recm: {}, recs: {}, sp: null };
  const W = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), wsnap);
  await tick(80);
  check('WING title', W.d.getElementById('title').textContent === 'WING Mixer');
  check('48 channel strips (40 ch + 8 aux)', W.d.querySelectorAll('#strips .strip').length === 48);
  check('8 mute groups', W.d.querySelectorAll('.mgbtn').length === 8);
  check('listen card visible', !W.d.getElementById('listen-card').classList.contains('none'));
  check('WING source tag', rowOf(W.d, 'ch/1').querySelector('.nm i').textContent.includes('CH 1 · Local 3'));
  check('WING status text', W.d.getElementById('status-text').textContent === 'WING');
  check('WING main 2 label', W.d.querySelectorAll('#master-card .strip')[1].querySelector('.nm i').textContent.includes('MAIN 2'));
  rowOf(W.d, 'ch/1').querySelector('.nm').click(); await tick();
  check('WING name tap opens the sheet', W.d.getElementById('sheet').classList.contains('open'));
  check('no script errors (WING)', W.errors.length === 0, W.errors);

  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})();
