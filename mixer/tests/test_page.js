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

  // ── Video card (v3.4) ──
  console.log('Video card');
  check('no camera -> Video card hidden', W.d.getElementById('cam-card').hidden === true);
  const camBase = { enabled: true, available: true, running: false, viewers: 0, audio: true, feed: 'bus1', delay_ms: 240,
                    quality: 'medium', max_delay: 3000, error: '', restarts: 0, audio_bitrate: '128k', audio_rates: ['64k', '96k', '128k', '160k'],
                    qualities: [{ id: 'high', label: 'High' }, { id: 'good', label: 'Good' }, { id: 'medium', label: 'Medium' }, { id: 'low', label: 'Low' }, { id: 'min', label: 'Minimum' }] };
  const vsnap = { ...wsnap, feeds: [{ id: 'main1', label: 'Main LR', usb: [1, 2] }, { id: 'bus1', label: 'Bus 1', usb: [3, 4] }], cam: camBase,
    listen: { running: false, listeners: 0, rtc: 0, error: '', pair: [0, 1], peak: [-120, -120], overruns: 0, opus_bitrate: '128k', opus_rates: ['64k', '96k', '128k', '160k'] } };
  const V = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), vsnap);
  await tick(80);
  const vd = V.d, el = id => vd.getElementById(id);
  check('camera present -> Video card shown', el('cam-card').hidden === false);
  check('5 picture tiers, saved one selected', el('cam-q').options.length === 5 && el('cam-q').value === 'medium', el('cam-q').value);
  check('4 sound bitrates for the video, saved one selected', el('cam-ab').options.length === 4 && el('cam-ab').value === '128k' && el('cam-ab').style.display === '', el('cam-ab').value);
  el('cam-ab').value = '64k'; el('cam-ab').dispatchEvent(new V.w.Event('change', { bubbles: true })); await tick(20);
  check('video sound bitrate change posts it', V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.audio_bitrate === '64k'));
  const lab = el('listen-ab');
  check('Listen card: low-latency bitrate choices, 128k selected', lab.options.length === 4 && lab.value === '128k' && lab.style.display === '', [lab.options.length, lab.value]);
  lab.value = '96k'; lab.dispatchEvent(new V.w.Event('change', { bubbles: true })); await tick(20);
  check('Listen bitrate change posts to /mixer/api/listen/set', V.posts.some(p => p.url === '/mixer/api/listen/set' && p.body && p.body.opus_bitrate === '96k'));
  el('mode-btn').click(); await tick(10);
  check('MP3-only mode hides the low-latency bitrate', lab.style.display === 'none');
  el('mode-btn').click(); await tick(10);
  check('pop-out button hidden where the browser cannot float video', el('cam-pip').style.display === 'none');
  check('audio feed choices follow the listen feeds, saved one selected', el('cam-feed').options.length === 2 && el('cam-feed').value === 'bus1', el('cam-feed').value);
  check('sync slider shows the saved delay and the max', el('cam-delay').value === '240' && el('cam-delay').max === '3000' && el('cam-delay-val').textContent === '240 ms', el('cam-delay-val').textContent);
  el('cam-up').click(); await tick(30);
  check('+ nudge: 250 ms, posted', el('cam-delay-val').textContent === '250 ms' && V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.delay_ms === 250), V.posts.slice(-2));
  el('cam-dn').click(); el('cam-dn').click(); await tick(30);
  check('- nudge twice: 230 ms', el('cam-delay-val').textContent === '230 ms', el('cam-delay-val').textContent);
  el('cam-delay').value = '300'; el('cam-delay').dispatchEvent(new V.w.Event('input', { bubbles: true }));
  check('slider drag updates the readout at once', el('cam-delay-val').textContent === '300 ms');
  await tick(260);
  check('slider drag posts the delay (debounced)', V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.delay_ms === 300));
  el('cam-feed').value = 'main1'; el('cam-feed').dispatchEvent(new V.w.Event('change', { bubbles: true })); await tick(20);
  check('feed change posts the feed', V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.feed === 'main1'));
  V.push({ t: 'cam', s: { ...camBase, feed: 'main1', quality: 'low', audio_bitrate: '160k', running: true, viewers: 2 } }); await tick(20);
  check('live status: feed + quality + sound follow the Pi', el('cam-feed').value === 'main1' && el('cam-q').value === 'low' && el('cam-ab').value === '160k');
  check('live status: viewers shown while not watching', el('cam-note').textContent === 'Camera live · 2 viewers', el('cam-note').textContent);
  el('cam-btn').click(); await tick(30);
  check('no WebRTC in the browser -> says so, resets the button', el('cam-note').textContent.includes('no WebRTC') && !el('cam-btn').classList.contains('on') && !el('cam-video').classList.contains('on'), el('cam-note').textContent);
  el('cam-snd').click();
  check('Sound toggle', el('cam-snd').title === 'Sound on' && el('cam-video').muted === false);
  el('cam-snd').click();
  check('Sound toggle back off', el('cam-snd').title === 'Sound off' && el('cam-video').muted === true);
  V.push({ t: 'cam', s: { ...camBase, available: false } }); await tick(20);
  check('camera unplugged -> card hides again', el('cam-card').hidden === true);
  // v3.8: camera list, rotation, camera mic, Wi-Fi cameras
  const srcs = [{ id: 'usb:nexigo', name: 'NexiGo N930E FHD Webcam', kind: 'usb', online: null, mic: true, auto: false, rotate: '0', fixed: false },
                { id: 'nabc123', name: 'Phone', kind: 'net', online: true, mic: true, auto: true, rotate: 'auto', fixed: false },
                { id: 'net0', name: 'Cfg cam', kind: 'net', online: false, mic: true, auto: false, rotate: '0', fixed: true }];
  const cam8 = { ...camBase, sources: srcs, choice: 'nabc123', active: 'nabc123', fallback: false, rotate: '90', rotate_mode: 'auto', mic_ok: true, epoch: 3, auto_note: '' };
  V.push({ t: 'cam', s: cam8 }); await tick(20);
  const so = [...el('cam-src').options].map(o => o.textContent);
  check('camera selector: USB + Wi-Fi cameras with online state, chosen one selected',
        so.length === 3 && so[0] === 'NexiGo N930E FHD Webcam' && so[1].includes('Phone · online') && so[2].includes('offline') && el('cam-src').value === 'nabc123', so);
  check('rotation choices include Auto for an IP Webcam, saved mode shown', [...el('cam-rot').options].map(o => o.value).join() === 'auto,0,90,180,270' && el('cam-rot').value === 'auto');
  check('camera mic offered as a sound source', [...el('cam-feed').options].some(o => o.value === 'mic' && o.textContent === 'Camera mic'));
  check('remove button shown for an added Wi-Fi camera', el('cam-del').style.display === '');
  el('cam-src').value = 'usb:nexigo'; el('cam-src').dispatchEvent(new V.w.Event('change', { bubbles: true })); await tick(20);
  check('choosing a camera posts it', V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.source === 'usb:nexigo'));
  el('cam-rot').value = '180'; el('cam-rot').dispatchEvent(new V.w.Event('change', { bubbles: true })); await tick(20);
  check('rotation change posts it', V.posts.some(p => p.url === '/mixer/api/cam/set' && p.body && p.body.rotate === '180'));
  V.push({ t: 'cam', s: { ...cam8, choice: 'net0', active: 'usb:nexigo', fallback: true, rotate_mode: '0', mic_ok: false, feed: 'mic' } }); await tick(20);
  check('chosen camera offline -> says what is shown instead; no Auto for a USB camera; mic marked missing',
        el('cam-note').textContent.includes('Cfg cam is offline · showing NexiGo') && ![...el('cam-rot').options].some(o => o.value === 'auto')
        && [...el('cam-feed').options].some(o => o.value === 'mic' && o.textContent.includes('not on this camera')) && el('cam-del').style.display === 'none', el('cam-note').textContent);
  V.push({ t: 'cam', s: { ...cam8, sources: [srcs[0]], choice: 'usb:nexigo', active: 'usb:nexigo' } }); await tick(20);
  check('only one camera -> no camera selector', el('cam-src').style.display === 'none');
  el('cam-add-btn').click();
  check('+ Camera opens the add panel', el('cam-add').hidden === false);
  el('cam-add-go').click(); await tick(10);
  check('Add with no address explains the format', el('cam-add-msg').textContent.includes('8080/video'));
  el('cam-add-name').value = 'Phone 2'; el('cam-add-url').value = 'http://192.168.1.77:8080/video';
  el('cam-add-go').click(); await tick(30);
  check('Add posts name + address + select', V.posts.some(p => p.url === '/mixer/api/cam/add' && p.body && p.body.name === 'Phone 2' && p.body.url === 'http://192.168.1.77:8080/video' && p.body.select === true));

  check('no script errors (video card)', V.errors.length === 0, V.errors);

  // a stubbed WebRTC handshake: video + audio recvonly, offer goes to the cam endpoint, DELETE on stop
  console.log('Video card: handshake (stubbed WebRTC)');
  const H = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), vsnap);
  await tick(80);
  const hd = H.d, hel = id => hd.getElementById(id), calls = [], seen = [];
  H.w.MediaStream = class { constructor() { this.t = []; } addTrack(t) { this.t.push(t); } getTracks() { return this.t; } };
  H.w.RTCPeerConnection = class {
    constructor(cfg) { this.cfg = cfg; this.iceGatheringState = 'complete'; this.connectionState = 'new'; H.w._pc = this; }
    addTransceiver(kind, o) { calls.push([kind, o.direction]); }
    async createOffer() { return { type: 'offer', sdp: 'v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n' }; }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) { calls.push(['answer', d.sdp.slice(0, 3)]); this.connectionState = 'connected'; this.onconnectionstatechange && this.onconnectionstatechange(); }
    async getStats() { return new Map(); }
    close() { calls.push(['close']); }
  };
  H.w.fetch = async (url, opts = {}) => {
    seen.push([url, opts.method || 'GET', opts.body]);
    if (url.includes('/api/rtc/config')) return { ok: true, status: 200, json: async () => ({ enabled: true, iceServers: [] }) };
    if (url.includes('/api/cam/whep')) return { ok: true, status: 201, text: async () => 'v=0 answer', headers: { get: () => '/mixer/api/cam/session/0123456789abcdef0123456789abcdef0123' } };
    return { ok: true, status: 200, json: async () => ({ ok: true }), headers: { get: () => null } };
  };
  hel('cam-btn').click(); await tick(60);
  check('offers video AND audio, receive only', calls.some(c => c[0] === 'video' && c[1] === 'recvonly') && calls.some(c => c[0] === 'audio' && c[1] === 'recvonly'), calls);
  check('offer posted to /mixer/api/cam/whep as SDP', seen.some(c => c[0] === '/mixer/api/cam/whep' && c[1] === 'POST' && String(c[2]).startsWith('v=0')), seen);
  check('answer applied; watching state', calls.some(c => c[0] === 'answer') && hel('cam-btn').classList.contains('on') && hel('cam-video').classList.contains('on'));
  check('the listen-back endpoints were not touched', !seen.some(c => c[0].includes('/api/rtc/whep') || c[0].includes('stream.mp3')));
  hel('cam-btn').click(); await tick(30);
  check('stop closes the connection and deletes the cam session', calls.some(c => c[0] === 'close') && seen.some(c => c[1] === 'DELETE' && c[0] === '/mixer/api/cam/session/0123456789abcdef0123456789abcdef0123'), seen.slice(-2));
  check('stop resets the button + hides the picture', !hel('cam-btn').classList.contains('on') && !hel('cam-video').classList.contains('on'));
  // v3.7: always one or the other -- Watch stops Listen (back when the video stops), Listen closes the video
  hel('listen-btn').click(); await tick(40);
  check('listening before the video', hel('listen-btn').classList.contains('on'));
  hel('cam-btn').click(); await tick(60);
  check('Watch stops Listen and turns the video sound on', !hel('listen-btn').classList.contains('on') && hel('cam-snd').title === 'Sound on' && hel('cam-video').muted === false);
  hel('cam-snd').click(); await tick(40);
  check('video Sound off is a plain mute (Listen stays off)', !hel('listen-btn').classList.contains('on') && hel('cam-video').muted === true && hel('cam-btn').classList.contains('on'));
  hel('cam-snd').click(); await tick(40);
  check('video Sound on again', hel('cam-video').muted === false);
  hel('listen-btn').click(); await tick(60);
  check('Listen pressed while watching -> the video closes, Listen plays',
        hel('listen-btn').classList.contains('on') && !hel('cam-btn').classList.contains('on') && !hel('cam-video').classList.contains('on')
        && seen.some(c => c[1] === 'DELETE' && c[0].includes('/api/cam/session/')));
  hel('listen-btn').click(); await tick(40);
  hel('cam-btn').click(); await tick(60);
  hel('cam-btn').click(); await tick(40);
  check('Stop video when Listen was off before -> stays quiet', !hel('listen-btn').classList.contains('on') && hel('cam-snd').title === 'Sound off');
  hel('listen-btn').click(); await tick(40);
  hel('cam-btn').click(); await tick(60);
  hel('cam-btn').click(); await tick(40);
  check('Stop video -> Listen resumes', hel('listen-btn').classList.contains('on') && !hel('cam-btn').classList.contains('on'));
  hel('listen-btn').click(); await tick(20);

  // pause / live
  hel('cam-btn').click(); await tick(60);
  const before = calls.filter(c => c[0] === 'answer').length;
  hel('cam-pause').click(); await tick(30);
  check('Pause: connection closed, picture kept on screen, button says Live',
        calls.filter(c => c[0] === 'close').length > 0 && hel('cam-btn').classList.contains('on') && hel('cam-video').classList.contains('on')
        && hel('cam-pause').title.includes('Live') && hel('cam-note').textContent.startsWith('Paused'), hel('cam-pause').title);
  hel('cam-pause').click(); await tick(60);
  check('Live: a fresh connection (now, not a backlog), button back to Pause',
        calls.filter(c => c[0] === 'answer').length === before + 1 && hel('cam-pause').title.includes('Pause'));
  hel('cam-pause').click(); await tick(30);
  hel('cam-btn').click(); await tick(40);
  check('Stop while paused resets Pause', hel('cam-pause').title.includes('Pause') && !hel('cam-video').classList.contains('on'));

  // fullscreen: the picture + controls block goes fullscreen; stopping leaves it
  let fsReq = 0, fsExit = 0;
  const pop0 = hel('cam-pop');
  pop0.requestFullscreen = async () => { fsReq++; Object.defineProperty(H.w.document, 'fullscreenElement', { value: pop0, configurable: true }); };
  H.w.document.exitFullscreen = async () => { fsExit++; Object.defineProperty(H.w.document, 'fullscreenElement', { value: null, configurable: true }); };
  hel('cam-fs').click(); await tick(10);
  check('Full before the video starts -> asks to start it first', fsReq === 0 && hel('cam-note').textContent.includes('Start the video'));
  hel('cam-btn').click(); await tick(60);
  hel('cam-fs').click(); await tick(10);
  check('Full -> the video block requests fullscreen', fsReq === 1);
  hel('cam-video').dispatchEvent(new H.w.MouseEvent('dblclick', { bubbles: true })); await tick(10);
  check('double-tap the picture -> leaves fullscreen', fsExit === 1);
  hel('cam-video').dispatchEvent(new H.w.MouseEvent('dblclick', { bubbles: true })); await tick(10);
  hel('cam-btn').click(); await tick(40);
  check('stopping the video leaves fullscreen', fsReq === 2 && fsExit === 2);

  // volume: no Web Audio -> plain element volume (capped at 100 %), saved per device
  const lv = hel('listen-vol');
  check('volume sliders start at 100 %', lv.value === '100' && hel('listen-vol-val').textContent === '100%' && hel('cam-vol-val').textContent === '100%');
  lv.value = '60'; lv.dispatchEvent(new H.w.Event('input', { bubbles: true })); await tick(10);
  check('Listen volume 60 % -> element volume 0.6, saved', Math.abs(hel('player').volume - 0.6) < 1e-6 && H.w.localStorage.getItem('mixer.vol.listen') === '0.6', hel('player').volume);
  lv.value = '250'; lv.dispatchEvent(new H.w.Event('input', { bubbles: true })); await tick(10);
  check('250 % without Web Audio: element at full, readout amber', hel('player').volume === 1 && hel('listen-vol-val').textContent === '250%' && hel('listen-vol-val').classList.contains('boost'));
  // volume boost with Web Audio: the video's audio track goes through gain -> limiter; element muted
  const nodes = [];
  class FakeNode { constructor(kind) { this.kind = kind; this.gain = { value: 1 }; ['threshold', 'knee', 'ratio', 'attack', 'release'].forEach(k => this[k] = { value: 0 }); this.out = []; nodes.push(this); }
    connect(n) { this.out.push(n); return n; } disconnect() { this.out = []; this.gone = true; } }
  H.w.AudioContext = class { constructor() { this.state = 'running'; this.destination = new FakeNode('dest'); }
    resume() { return Promise.resolve(); }
    createMediaStreamSource(st) { const n = new FakeNode('src'); n.stream = st; return n; }
    createGain() { return new FakeNode('gain'); } createDynamicsCompressor() { return new FakeNode('comp'); } };
  H.w.MediaStream.prototype.getAudioTracks = function () { return this.t.filter(t => t.kind === 'audio'); };
  const cv = hel('cam-vol');
  cv.value = '300'; cv.dispatchEvent(new H.w.Event('input', { bubbles: true })); await tick(10);
  hel('cam-btn').click(); await tick(60);
  H.w._pc.ontrack({ track: { kind: 'audio', stop() {} } }); await tick(10);
  const gain = nodes.find(n => n.kind === 'gain' && !n.gone), comp = nodes.find(n => n.kind === 'comp' && !n.gone);
  check('video at 300 %: stream -> gain 3.0 -> limiter -> speakers, element muted',
        gain && gain.gain.value === 3 && comp && comp.out[0] && comp.out[0].kind === 'dest' && comp.ratio.value === 20 && hel('cam-video').muted === true, gain && gain.gain.value);
  hel('cam-snd').click(); await tick(10);
  check('Sound off while boosted -> gain 0', gain.gain.value === 0);
  hel('cam-snd').click(); await tick(10);
  cv.value = '100'; cv.dispatchEvent(new H.w.Event('input', { bubbles: true })); await tick(10);
  check('back to 100 % -> Web Audio released, element plays unmuted', gain.gone && hel('cam-video').muted === false);
  cv.value = '200'; cv.dispatchEvent(new H.w.Event('input', { bubbles: true })); await tick(10);
  const g2 = nodes.filter(n => n.kind === 'gain' && !n.gone).pop();
  check('raised again -> boosted again (2.0)', g2 && g2.gain.value === 2 && hel('cam-video').muted === true);
  Object.defineProperty(H.w.document, 'hidden', { value: true, configurable: true });
  H.w.document.dispatchEvent(new H.w.Event('visibilitychange')); await tick(10);
  check('page in the background -> plain playback (keeps playing if Web Audio is suspended)', g2.gone && hel('cam-video').muted === false);
  Object.defineProperty(H.w.document, 'hidden', { value: false, configurable: true });
  H.w.document.dispatchEvent(new H.w.Event('visibilitychange')); await tick(10);
  check('back in front -> boosted again', nodes.filter(n => n.kind === 'gain' && !n.gone).length === 1 && hel('cam-video').muted === true);
  hel('cam-btn').click(); await tick(40);
  check('stop -> boost released', nodes.filter(n => n.kind === 'gain' && !n.gone).length === 0);
  cv.value = '100'; cv.dispatchEvent(new H.w.Event('input', { bubbles: true }));
  delete H.w.AudioContext;

  // pop out: a browser with standard Picture-in-Picture
  let pipCalls = 0;
  H.w.document.pictureInPictureEnabled = true;
  hel('cam-video').requestPictureInPicture = async () => { pipCalls++; Object.defineProperty(H.w.document, 'pictureInPictureElement', { value: hel('cam-video'), configurable: true }); };
  H.w.document.exitPictureInPicture = async () => { Object.defineProperty(H.w.document, 'pictureInPictureElement', { value: null, configurable: true }); };
  hel('cam-btn').click(); await tick(60);
  check('pop-out button offered once PiP is available', hel('cam-pip').style.display === '', [hel('cam-pip').style.display, H.w.document.pictureInPictureEnabled, hel('cam-btn').className, typeof hel('cam-video').requestPictureInPicture]);
  hel('cam-pip').click(); await tick(30);
  check('Pop out -> Picture-in-Picture requested, button says Pop in', pipCalls === 1 && hel('cam-pip').title.includes('Pop in'), hel('cam-pip').title);
  hel('cam-btn').click(); await tick(160);
  check('stopping the video leaves Picture-in-Picture', !H.w.document.pictureInPictureElement && hel('cam-pip').title.includes('Pop out'), hel('cam-pip').title);
  // pop out in Chrome desktop: Document Picture-in-Picture -- the player AND the sync controls move
  const PW = new JSDOM('<!doctype html><html><head></head><body></body></html>');
  let pwHide = null;
  const pw = { document: PW.window.document, addEventListener: (ev, fn) => { if (ev === 'pagehide') pwHide = fn; }, close() { pwHide && pwHide(); } };
  H.w.documentPictureInPicture = { requestWindow: async () => pw };
  hel('cam-btn').click(); await tick(60);
  hel('cam-pip').click(); await tick(40);
  const pdoc = PW.window.document;
  check('Chrome pop-out window holds the video + sync controls', !!pdoc.getElementById('cam-video') && !!pdoc.getElementById('cam-delay') && !hd.getElementById('cam-pop')
        && hel('cam-pip-ph').classList.contains('on') && pdoc.body.className === 'pipwin' && pdoc.querySelectorAll('style').length > 0);
  pdoc.getElementById('cam-up').click(); await tick(30);
  check('sync nudge works inside the pop-out window', pdoc.getElementById('cam-delay-val').textContent === (+pdoc.getElementById('cam-delay').value) + ' ms'
        && seen.some(c => c[0] === '/mixer/api/cam/set' && String(c[2]).includes('delay_ms')));
  H.push({ t: 'cam', s: { ...camBase, running: true, viewers: 1, delay_ms: 500 } }); await tick(1300);
  H.push({ t: 'cam', s: { ...camBase, running: true, viewers: 1, delay_ms: 500 } }); await tick(20);
  check('live status still paints controls in the pop-out window', pdoc.getElementById('cam-delay-val').textContent === '500 ms', pdoc.getElementById('cam-delay-val').textContent);
  hel('cam-btn').click(); await tick(60);
  check('stop: the player goes back into the card, window closed', !!hd.getElementById('cam-pop') && !pdoc.getElementById('cam-pop') && !hel('cam-pip-ph').classList.contains('on'));
  delete H.w.documentPictureInPicture;
  // v3.8: the Pi restarted the encoder (new epoch) -> the page rejoins by itself; old epoch -> nothing
  hel('cam-btn').click(); await tick(60);
  H.push({ t: 'cam', s: { ...camBase, running: true, epoch: 5 } }); await tick(20);
  const answers0 = calls.filter(c => c[0] === 'answer').length;
  H.push({ t: 'cam', s: { ...camBase, running: true, epoch: 6 } }); await tick(400);
  check('encoder restarted on the Pi (epoch changed) -> reconnects once', calls.filter(c => c[0] === 'answer').length === answers0 + 1);
  H.push({ t: 'cam', s: { ...camBase, running: true, epoch: 6 } }); await tick(400);
  check('same epoch again -> no extra reconnect', calls.filter(c => c[0] === 'answer').length === answers0 + 1);
  // controls on the picture fade after 3 s, come back on touch
  const stage = hel('cam-stage');
  check('overlay controls live on the picture', stage.contains(hel('cam-pause')) && stage.contains(hel('cam-fs')) && stage.contains(hel('cam-pip')) && stage.contains(hel('cam-snd')));
  await tick(3200);
  check('overlay fades after 3 s', stage.classList.contains('idle'));
  stage.dispatchEvent(new H.w.Event('pointerdown', { bubbles: true }));
  check('touch brings the overlay back', !stage.classList.contains('idle'));
  // the floating window's own pause / play: connection kept, play = live
  const closes0 = calls.filter(c => c[0] === 'close').length;
  Object.defineProperty(H.w.document, 'pictureInPictureElement', { value: hel('cam-video'), configurable: true });
  hel('cam-video').srcObject = hel('cam-video').srcObject || {};
  hel('cam-video').dispatchEvent(new H.w.Event('pause')); await tick(10);
  check('pause from the floating window: shown as paused, connection kept', hel('cam-pause').title.includes('Live') && calls.filter(c => c[0] === 'close').length === closes0);
  hel('cam-video').dispatchEvent(new H.w.Event('play')); await tick(10);
  check('play from the floating window -> live again', hel('cam-pause').title === 'Pause');
  Object.defineProperty(H.w.document, 'pictureInPictureElement', { value: null, configurable: true });
  hel('cam-btn').click(); await tick(40);

  check('no script errors (video handshake)', H.errors.length === 0, H.errors);

  // picture only: no console feeds (X32 for now)
  console.log('Video card: picture only (no console audio)');
  const N = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), { ...wsnap, feeds: [], cam: { ...camBase, audio: false } });
  await tick(80);
  check('no feeds -> audio feed choice and Sound toggle hidden', N.d.getElementById('cam-feed').style.display === 'none' && N.d.getElementById('cam-snd').style.display === 'none');
  check('card still shown (video works without console audio)', N.d.getElementById('cam-card').hidden === false);
  N.push({ t: 'cam', s: { ...camBase, audio: false, running: true, viewers: 1, audio_issue: 'capture ended' } }); await tick(20);
  check('picture-only after a capture failure says why', N.d.getElementById('cam-note').textContent === 'Camera live · 1 viewer · picture only: capture ended', N.d.getElementById('cam-note').textContent);

  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})();
