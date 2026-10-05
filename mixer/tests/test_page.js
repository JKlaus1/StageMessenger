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

function load(html, snap, apiMap) {
  const posts = [], errors = [];
  let es = null;
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => { if (!/Not implemented: (navigation|HTMLMediaElement)/.test(e.message)) errors.push(e.message); });
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://pi.local/mixer', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (url, opts = {}) => {
        let body = null; try { body = opts.body ? JSON.parse(opts.body) : null; } catch (e) {}
        posts.push({ url, body });
        const j = url.includes('/api/state') ? snap : (apiMap && apiMap[url]) ? apiMap[url]
          : url.includes('/api/node') ? { ok: false, path: '', params: [] } : { ok: true, action: 'override', state: 0 };
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
  const API = JSON.parse(fs.readFileSync(path.join(FX, 'x32_api.json')));
  const P = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), snap, API);
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
  d.getElementById('solo-clear').click(); await tick();

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

  // ── v3.1 channel sheet ──
  console.log('X32 channel sheet');
  const vis = id => d.getElementById(id).style.display !== 'none';
  const tabVis = t => d.querySelector(`#sh-tabs .tab[data-tab="${t}"]`).style.display !== 'none';
  P.posts.length = 0;
  rowOf(d, 'ch/1').querySelector('.nm').click(); await tick(120);
  check('name tap opens the sheet', d.getElementById('sheet').classList.contains('open'));
  check('all four tabs on a channel', ['input', 'eq', 'gate', 'dyn'].every(tabVis));
  check('no high cut row, no source-polarity button', !vis('row-hcf') && !vis('sh-spol'));
  check('low cut row shown', vis('row-lcf'));
  check('no stagebox -> "No preamp" note', !vis('sh-pre') && vis('sh-nopre'));
  check('source line', d.getElementById('sh-src').textContent.startsWith('AES A 1'), d.getElementById('sh-src').textContent);
  const out = id => d.querySelector(`#${id} output`).textContent;
  check('trim readout +15.0 dB', out('row-trim') === '+15.0 dB', out('row-trim'));
  check('low cut readout 46 Hz', out('row-lcf') === '46 Hz', out('row-lcf'));
  check('tab dots fetched (eq/gate/dyn)', ['eq', 'gate', 'dyn'].every(t => P.posts.some(p => p.url.includes('path=%2Fch%2F1%2F' + t))));
  check('EQ dot on, Gate dot on, Comp dot off', d.querySelector('.tab[data-tab="eq"] .dot').classList.contains('on')
        && d.querySelector('.tab[data-tab="gate"] .dot').classList.contains('on') && !d.querySelector('.tab[data-tab="dyn"] .dot').classList.contains('on'));
  P.posts.length = 0;
  d.querySelector('#row-trim .nud[data-d="0.5"]').click(); await tick();
  const tp = P.posts.find(p => p.url === '/mixer/api/set');
  check('trim + posts 15.5', tp && tp.body.a === '/ch/1/in/set/trim' && tp.body.v === 15.5, P.posts);
  P.push({ t: 'upd', a: '/io/in/A/1/g', v: 15.5 }); P.push({ t: 'upd', a: '/io/in/A/1/vph', v: 1 }); await tick();
  check('stagebox appears -> preamp gain shown', vis('sh-pre') && out('row-gain') === '15.5 dB', out('row-gain'));
  check('48V lit', d.getElementById('sh-48v').classList.contains('on'));
  P.posts.length = 0;
  d.querySelector('#row-gain .nud[data-d="0.5"]').click(); await tick();
  const gp = P.posts.find(p => p.url === '/mixer/api/set');
  check('gain + posts /io/in/A/1/g 16', gp && gp.body.a === '/io/in/A/1/g' && gp.body.v === 16, P.posts);
  // EQ tab
  d.querySelector('#sh-tabs .tab[data-tab="eq"]').click(); await tick(120);
  check('EQ curve view (not generic)', vis('eq-std') && !vis('eq-generic'));
  check('4 band buttons', d.querySelectorAll('#eq-bands .btn').length === 4);
  const sel = d.querySelector('#eq-band-ctl select');
  check('band type selector with M32 types', sel && Array.from(sel.options).map(o => o.value).join() === 'LCut,LShv,PEQ,VEQ,HShv,HCut' && sel.value === 'PEQ',
        sel && Array.from(sel.options).map(o => o.value));
  const eqOuts = Array.from(d.querySelectorAll('#eq-band-ctl output')).map(o => o.textContent);
  check('band 1 readouts gain/freq/Q', eqOuts[0] === '+3.0 dB' && eqOuts[1] === '58 Hz' && /^1\.9|^2\.0/.test(eqOuts[2]), eqOuts);
  P.posts.length = 0;
  sel.value = 'HShv'; sel.dispatchEvent(new P.w.Event('change')); await tick();
  const tq = P.posts.find(p => p.url === '/mixer/api/nodeset');
  check('type change posts nodeset 1type HShv', tq && tq.body.path === '/ch/1/eq' && tq.body.key === '1type' && tq.body.value === 'HShv', P.posts);
  d.querySelector('#eq-bands .btn[data-b="4"]').click(); await tick();
  check('band 4 shows VEQ', d.querySelector('#eq-band-ctl select').value === 'VEQ');
  // Gate / Comp tabs
  d.querySelector('#sh-tabs .tab[data-tab="gate"]').click(); await tick(120);
  const gl = Array.from(d.querySelectorAll('#gate-params .gp span')).map(x => x.textContent);
  check('gate controls', ['Mode', 'Thresh', 'Range', 'Attack', 'Hold', 'Release', 'Key filter', 'Filter', 'Filter freq'].every(l => gl.includes(l)), gl);
  check('gate ON button', d.getElementById('gate-on').textContent === 'Gate ON');
  d.querySelector('#sh-tabs .tab[data-tab="dyn"]').click(); await tick(120);
  const dl = Array.from(d.querySelectorAll('#dyn-params .gp span')).map(x => x.textContent);
  check('comp controls', ['Mode', 'Detect', 'Env', 'Thresh', 'Ratio', 'Knee', 'Makeup', 'Attack', 'Hold', 'Release', 'Position', 'Mix', 'Auto'].every(l => dl.includes(l)), dl);
  const ratio = Array.from(d.querySelectorAll('#dyn-params .gp')).find(g => g.querySelector('span').textContent === 'Ratio').querySelector('select');
  check('ratio list 1.1 .. 100, value 1.5', ratio.options.length === 12 && ratio.value === '1.5');
  P.push({ t: 'm', c, a, cd: c.map(() => [-20, 50, -20, 20, 30, 6.5]), ad: a.map(() => [0, 0, 0, 0]), b: Array(16).fill(-30), m: [-99, -99] }); await tick();
  check('GR shown in dB on X32', d.getElementById('dyn-grpct').textContent === '−6.5 dB', d.getElementById('dyn-grpct').textContent);
  // source picker
  d.querySelector('#sh-tabs .tab[data-tab="input"]').click(); await tick();
  d.getElementById('sh-change').click(); await tick(80);
  const groups = Array.from(d.querySelectorAll('#sh-groups .btn')).map(b => b.textContent);
  check('picker groups In/Aux/USB/FX/Bus', groups.join() === 'In,Aux,USB,FX,Bus', groups);
  const pins = d.querySelectorAll('#sh-inputs .pin');
  check('In list 32, In 1 = AES A 1 marked current', pins.length === 32 && pins[0].querySelector('span').textContent === 'AES A 1' && pins[0].classList.contains('cur'));
  P.posts.length = 0;
  pins[4].click(); await tick();
  const pp = P.posts.find(p => p.url === '/mixer/api/patch');
  check('patch posts IN 5', pp && pp.body.grp === 'IN' && pp.body.in === 5 && pp.body.kind === 'ch' && pp.body.n === 1, P.posts);
  d.getElementById('sh-close').click(); await tick();
  // aux strip: EQ only
  rowOf(d, 'aux/1').querySelector('.nm').click(); await tick(120);
  check('aux sheet: input + EQ only', tabVis('input') && tabVis('eq') && !tabVis('gate') && !tabVis('dyn'));
  check('aux sheet: no low cut', !vis('row-lcf'));
  check('aux sheet did not fetch gate/dyn', !P.posts.some(p => /path=%2Faux%2F1%2F(gate|dyn)/.test(p.url)));
  d.getElementById('sh-close').click(); await tick();
  // ── v3.2 X-LIVE recorder ──
  console.log('X32 recorder (X-LIVE)');
  const rc = d.getElementById('rec-card');
  check('recorder card shown', !rc.classList.contains('none'));
  check('one SD row (A)', d.querySelectorAll('#rec-rows .rec-row').length === 1
        && d.querySelector('#rec-rows .rec-id b').textContent === 'A');
  check('free time from the SD info', /1 h 23 m free/.test(d.querySelector('#rec-rows .rec-info').textContent),
        d.querySelector('#rec-rows .rec-info').textContent);
  check('card auto selectors hidden', d.querySelector('.auto-row').style.display === 'none');
  check('routing switch reads LIVE / PLAYBACK', Array.from(d.querySelectorAll('#alt-seg button')).map(x => x.textContent).join() === 'LIVE,PLAYBACK');
  const opts = Array.from(d.querySelectorAll('#rec-rows .pb-sess option')).map(o => o.textContent);
  check('session picker in console order', opts.length === 2 && opts[0] === '#1  5 Oct 2026 · 10:53:06' && opts[1].startsWith('#2'), opts);
  check('open session #2 selected', d.querySelector('#rec-rows .pb-sess').value === '2');
  check('marker chips of the open session', Array.from(d.querySelectorAll('#rec-rows .pb-marks button')).map(x => x.textContent).join() === '◆1 0:03,◆2 0:09');
  check('move-marker button hidden', d.querySelector('#rec-rows .pb-medit .mv').style.display === 'none');
  check('hint does not offer "move"', !/move or/.test(d.querySelector('#rec-rows .pb-hint').textContent));
  P.posts.length = 0;
  d.querySelector('#rec-rows .rec-go').click(); await tick();
  const rp = P.posts.find(p => p.url === '/mixer/api/rec');
  check('REC posts /api/rec card 1', rp && rp.body.action === 'rec' && rp.body.card === 1, P.posts);
  P.posts.length = 0;
  d.querySelector('#rec-rows .pb-marks button').click(); await tick();
  const gp2 = P.posts.find(p => p.url === '/mixer/api/play');
  check('marker chip posts goto 1', gp2 && gp2.body.action === 'goto' && gp2.body.n === 1, P.posts);
  P.push({ t: 'upd', a: '/cards/wlive/1/$stat/state', v: 'PLAY' }); await tick();
  check('PLAY badge + pause button', d.querySelector('#rec-rows .rec-badge').textContent === 'PLAY'
        && d.querySelector('#rec-rows .pb-play').textContent.includes('PAUSE'));
  P.push({ t: 'upd', a: '/cards/wlive/1/$stat/state', v: 'STOP' }); await tick();
  // ── v3.3 console search ──
  console.log('console search (v3.3)');
  const stx = () => d.getElementById('status-text').textContent;
  P.push({ t: 'snap', ...snap, conn: false, loaded: false, found: false }); await tick();
  check('searching -> "Looking for a console…"', stx() === 'Looking for a console…', stx());
  P.push({ t: 'snap', ...snap, conn: false, loaded: false, found: true }); await tick();
  check('found but gone -> "M32C offline"', stx() === 'M32C offline', stx());
  P.push({ t: 'snap', ...snap }); await tick();
  check('back -> model', stx() === 'M32C', stx());
  check('no script errors (X32)', P.errors.length === 0, P.errors);

  // caps mismatch -> one reload attempt (guarded)
  const P3 = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), { ...snap, caps: { ...snap.caps, console: 'wing', nch: 40 } });
  await tick(80);
  check('console change in snapshot -> reload guard set', P3.w.sessionStorage.getItem('mixer.capsReload') === '1');
  const P4 = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), { ...snap, caps: { ...snap.caps, model: 'X32' } });
  await tick(80);
  check('model change (found the real console) -> reload', P4.w.sessionStorage.getItem('mixer.capsReload') === '1');

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
  check('WING recorder hidden without a WING-LIVE card', W.d.getElementById('rec-card').classList.contains('none'));
  check('WING auto selectors kept', W.d.querySelector('.auto-row').style.display !== 'none');
  check('WING high cut + source polarity shown', W.d.getElementById('row-hcf').style.display !== 'none' && W.d.getElementById('sh-spol').style.display !== 'none');
  check('WING all tabs on aux', (() => { W.d.getElementById('sh-close').click(); rowOf(W.d, 'aux/1').querySelector('.nm').click();
    return ['eq', 'gate', 'dyn'].every(t => W.d.querySelector(`#sh-tabs .tab[data-tab="${t}"]`).style.display !== 'none'); })());
  check('no script errors (WING)', W.errors.length === 0, W.errors);

  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})();
