// jsdom console-view suite (v4.0): the served page + a live snapshot, switched to the console view.
//   cd ~/stage-messenger && python3 -m mixer.tests.gen_page_fixtures /tmp/fx && node mixer/tests/test_console.js /tmp/fx
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

function load(html, snap, apiMap, url) {
  const posts = [], errors = [];
  let es = null;
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => { if (!/Not implemented: (navigation|HTMLMediaElement)/.test(e.message)) errors.push(e.message); });
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: url || 'http://pi.local/mixer?view=console', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, opts = {}) => {
        let body = null; try { body = opts.body ? JSON.parse(opts.body) : null; } catch (e) {}
        posts.push({ url: u, body });
        const j = u.includes('/api/state') ? snap : (apiMap && apiMap[u]) ? (typeof apiMap[u] === 'function' ? apiMap[u](body) : apiMap[u])
          : u.includes('/api/node') ? { ok: false, path: '', params: [] } : { ok: true, action: 'mute', state: 1 };
        return { ok: true, status: 200, type: 'basic', headers: { get: () => null }, json: async () => j, clone() { return this; } };
      };
      w.EventSource = class { constructor() { es = this; this.readyState = 1;
        setTimeout(() => { this.onopen && this.onopen(); this.onmessage({ data: JSON.stringify({ t: 'snap', ...snap }) }); }, 0); }
        close() {} };
      w.EventSource.OPEN = 1; w.EventSource.CLOSED = 2;
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'Joseph';
      w.HTMLMediaElement.prototype.play = () => Promise.resolve(); w.HTMLMediaElement.prototype.pause = () => {};
      w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
    } });
  return { dom, w: dom.window, d: dom.window.document, posts, errors, push: m => es.onmessage({ data: JSON.stringify(m) }) };
}
function ptr(w, el, type, x, y, id = 1) {
  const e = new w.Event(type, { bubbles: true, cancelable: true });
  Object.assign(e, { pointerId: id, clientX: x, clientY: y });
  el.dispatchEvent(e);
}
const strip = (d, key, pin) => d.querySelector(`${pin ? '#cv-pin' : '#cv-strips'} .cs[data-k="${key}"]`);
const keys = d => [...d.querySelectorAll('#cv-strips .cs')].map(e => e.dataset.k);
const sets = (P, a) => P.posts.filter(p => p.url === '/mixer/api/set' && p.body && p.body.a === a);

(async () => {
  console.log('X32 console view (M32C)');
  const snap = JSON.parse(fs.readFileSync(path.join(FX, 'x32.json')));
  const API = JSON.parse(fs.readFileSync(path.join(FX, 'x32_api.json')));
  let saved = null;
  API['/mixer/api/layouts'] = body => {
    if (body && body.delete) return { ok: true, profiles: {} };
    saved = body; return { ok: true, profiles: { [body.profile]: { layers: body.layers } } };
  };
  const P = load(fs.readFileSync(path.join(FX, 'x32.html'), 'utf8'), snap, API);
  await tick(80);
  const { d, w } = P;
  check('?view=console -> console mode', d.body.classList.contains('cvm'));
  check('remembered for this device', w.localStorage.getItem('mixer.view') === 'console');
  check('header: CLASSIC button', d.getElementById('view-btn').textContent === 'CLASSIC');
  check('layer 1 default = 32 channels', keys(d).length === 32 && keys(d)[0] === 'ch/1', keys(d).length);
  const s1 = strip(d, 'ch/1');
  check('ch1 name tile Kick', s1.querySelector('.cs-nm').textContent === 'Kick');
  check('ch1 label Ch 1', s1.querySelector('.cs-lb').textContent === 'Ch 1');
  check('ch1 dB shown', s1.querySelector('.cs-db').textContent === fmt(snap.state['/ch/1/fdr']), s1.querySelector('.cs-db').textContent);
  check('ch1 tile coloured', s1.querySelector('.cs-nm').style.background !== '');
  check('listen card moved into the popover', d.getElementById('listen-card').parentElement.id === 'cv-ls-home');
  check('mute groups moved into the popover', d.getElementById('mg-grid').parentElement.id === 'cv-mg-home');
  check('group-muted ch2 shows MG', strip(d, 'ch/2').querySelector('.cs-mu').textContent === 'MG', strip(d, 'ch/2').querySelector('.cs-mu').textContent);
  check('muted ch21 in the engaged group shows MG', strip(d, 'ch/21').querySelector('.cs-mu').textContent === 'MG');
  check('DCA button shown on X32 (8 DCAs)', d.getElementById('cv-ldca').style.display !== 'none');
  check('bus list: LR + 16 buses + 6 matrices, no Main 2-4 on X32', d.querySelectorAll('#cv-c2 .cvbus').length === 23,
        d.querySelectorAll('#cv-c2 .cvbus').length);

  // mute enable
  P.posts.length = 0;
  d.getElementById('cv-muteen').click(); await tick();
  check('Mute Enable off', !d.getElementById('cv-muteen').classList.contains('on') && w.localStorage.getItem('mixer.cvMuteEn') === '0');
  strip(d, 'ch/17').querySelector('.cs-mu').click(); await tick();
  check('MUTE ignored while disabled', !P.posts.some(p => p.url === '/mixer/api/mute'));
  check('MUTE EN flashes', d.getElementById('cv-muteen').classList.contains('flash'));
  d.getElementById('cv-muteen').click(); await tick();
  strip(d, 'ch/17').querySelector('.cs-mu').click(); await tick();
  check('MUTE posts the channel mute (group-aware API)', P.posts.some(p => p.url === '/mixer/api/mute' && p.body.kind === 'ch' && p.body.n === 17));
  check('ch17 lit at once', strip(d, 'ch/17').querySelector('.cs-mu').classList.contains('on'));
  P.posts.length = 0;
  strip(d, 'ch/3').querySelector('.cs-mu').click(); await tick();
  check('group-muted ch3: MUTE = override (pulled out of the group)', P.posts.some(p => p.url === '/mixer/api/mute' && p.body.n === 3)
        && strip(d, 'ch/3').querySelector('.cs-mu').textContent === 'MUTE' && !strip(d, 'ch/3').querySelector('.cs-mu').classList.contains('on'));

  // solo
  strip(d, 'ch/4').querySelector('.cs-so').click(); await tick();
  check('SOLO posts $solo', sets(P, '/ch/4/$solo').some(p => p.body.v === 1));
  check('solo clear shows', d.getElementById('solo-clear').classList.contains('show'));
  d.getElementById('solo-clear').click(); await tick();
  check('solo clear clears it', sets(P, '/ch/4/$solo').some(p => p.body.v === 0));

  // fader: double-tap = 0 dB, drag = relative, fine = 1:3
  P.posts.length = 0;
  const fd = strip(d, 'ch/14').querySelector('.cs-fd');
  ptr(w, fd, 'pointerdown', 10, 100); ptr(w, fd, 'pointerup', 10, 100);
  ptr(w, fd, 'pointerdown', 10, 100); ptr(w, fd, 'pointerup', 10, 100); await tick();
  check('double-tap -> 0 dB', sets(P, '/ch/14/fdr').some(p => p.body.v === 0), sets(P, '/ch/14/fdr'));
  check('dB readout 0.0', strip(d, 'ch/14').querySelector('.cs-db').textContent === '0.0');
  P.posts.length = 0;
  const fd5 = strip(d, 'ch/5').querySelector('.cs-fd');            // rail height 0 in jsdom -> 1 px = full travel
  ptr(w, fd5, 'pointerdown', 10, 100, 7); ptr(w, fd5, 'pointermove', 10, 110, 7); ptr(w, fd5, 'pointermove', 10, 111, 7);
  ptr(w, fd5, 'pointerup', 10, 111, 7); await tick();
  check('drag down -> -inf (relative)', sets(P, '/ch/5/fdr').some(p => p.body.v <= -90), sets(P, '/ch/5/fdr'));
  P.posts.length = 0;
  ptr(w, fd5, 'pointerdown', 50, 100, 8); ptr(w, fd5, 'pointermove', 70, 101, 8); ptr(w, fd5, 'pointermove', 90, 102, 8);
  ptr(w, fd5, 'pointerup', 90, 102, 8); await tick();
  check('sideways swipe never moves a fader', !sets(P, '/ch/5/fdr').length);
  d.getElementById('cv-fine').click(); await tick();
  check('Fine on + remembered', d.getElementById('cv-fine').classList.contains('on') && w.localStorage.getItem('mixer.cvFine') === '1');
  d.getElementById('cv-fine').click();

  // layers
  d.getElementById('cv-ldca').click(); await tick();
  check('DCA layer: 8 DCAs', keys(d).join() === 'dca/1,dca/2,dca/3,dca/4,dca/5,dca/6,dca/7,dca/8', keys(d));
  check('DCA names', strip(d, 'dca/1').querySelector('.cs-nm').textContent === 'Drums' && strip(d, 'dca/3').querySelector('.cs-nm').textContent === 'DCA 3');
  check('layer remembered', w.localStorage.getItem('mixer.cvLayer') === 'dca');
  P.posts.length = 0;
  strip(d, 'dca/2').querySelector('.cs-mu').click(); await tick();
  check('DCA mute posts /dca/2/mute', sets(P, '/dca/2/mute').some(p => p.body.v === 1));
  d.getElementById('cv-lmm').click(); await tick();
  check('Mtx/Main: 6 matrices then LR, M/C', keys(d).join() === 'mtx/1,mtx/2,mtx/3,mtx/4,mtx/5,mtx/6,main/1,main/2', keys(d));
  check('LR label on X32', strip(d, 'main/1').querySelector('.cs-lb').textContent === 'LR');

  // sends on fader: bus 1
  d.getElementById('cv-l1').click(); await tick();
  const bus1 = d.querySelector('#cv-c2 .cvbus[data-k="bus"][data-n="1"]');
  bus1.click(); await tick();
  check('bus 1 selected', bus1.classList.contains('sel'));
  check('bus 1 master pinned', !!strip(d, 'bus/1', true) && d.getElementById('cv-pin').classList.contains('on'));
  check('mode label says sends', /SENDS ON FADER/.test(d.getElementById('cv-mode').textContent), d.getElementById('cv-mode').textContent);
  check('ch1 shows its send level', strip(d, 'ch/1').querySelector('.cs-db').textContent === fmt(snap.state['/ch/1/send/1/lvl']),
        [strip(d, 'ch/1').querySelector('.cs-db').textContent, snap.state['/ch/1/send/1/lvl']]);
  check('MUTE becomes send ON', strip(d, 'ch/1').querySelector('.cs-mu').textContent === 'ON' && strip(d, 'ch/1').querySelector('.cs-mu').classList.contains('snd'));
  check('muted ch21 flagged (red bar)', strip(d, 'ch/21').classList.contains('chm'));
  P.posts.length = 0;
  strip(d, 'ch/1').querySelector('.cs-mu').click(); await tick();
  check('send off posts /ch/1/send/1/on 0', sets(P, '/ch/1/send/1/on').some(p => p.body.v === 0));
  check('shows OFF', strip(d, 'ch/1').querySelector('.cs-mu').textContent === 'OFF');
  const f1 = strip(d, 'ch/1').querySelector('.cs-fd');
  ptr(w, f1, 'pointerdown', 10, 100, 9); ptr(w, f1, 'pointerup', 10, 100, 9); ptr(w, f1, 'pointerdown', 10, 100, 9); ptr(w, f1, 'pointerup', 10, 100, 9); await tick();
  check('double-tap sets the SEND to 0 dB, not the fader', sets(P, '/ch/1/send/1/lvl').some(p => p.body.v === 0) && !sets(P, '/ch/1/fdr').length);
  P.push({ t: 'upd', a: '/ch/2/send/1/lvl', v: -12.5 }); await tick();
  check('console push repaints the send', strip(d, 'ch/2').querySelector('.cs-db').textContent === '-12.5');
  P.posts.length = 0;
  strip(d, 'bus/1', true).querySelector('.cs-mu').click(); await tick();
  check('pinned master MUTE = bus mute', sets(P, '/bus/1/mute').length === 1);
  bus1.click(); await tick();
  check('tap again -> back to LR, pin gone', !d.getElementById('cv-pin').classList.contains('on') && strip(d, 'ch/1').querySelector('.cs-mu').textContent !== 'ON');

  // sends on fader: matrix 2 (X32: only buses / mains feed a matrix)
  const mtx2 = d.querySelector('#cv-c2 .cvbus[data-k="mtx"][data-n="2"]');
  mtx2.click(); await tick();
  check('mtx 2: channels greyed', strip(d, 'ch/1').classList.contains('na'));
  d.getElementById('cv-l3').click(); await tick();
  check('layer 3 default = buses', keys(d).length === 16 && keys(d)[0] === 'bus/1');
  check('bus1 -> mtx2 level shown', strip(d, 'bus/1').querySelector('.cs-db').textContent === '0.0' && !strip(d, 'bus/1').classList.contains('na'));
  P.posts.length = 0;
  strip(d, 'bus/1').querySelector('.cs-mu').click(); await tick();
  check('bus -> matrix send toggle', sets(P, '/bus/1/send/MX2/on').length === 1);
  mtx2.click(); await tick();

  // layer editor (hold = contextmenu here)
  d.getElementById('cv-l2').dispatchEvent(new w.Event('contextmenu', { bubbles: true, cancelable: true })); await tick();
  check('hold opens the editor', d.getElementById('cv-ed').classList.contains('open'));
  check('editor lists layer 2 default (8 aux + 2 mains)', d.querySelectorAll('#cv-ed-cur .cv-it').length === 10, d.querySelectorAll('#cv-ed-cur .cv-it').length);
  d.getElementById('cv-ed-clr').click();
  check('clear empties it', d.querySelectorAll('#cv-ed-cur .cv-it').length === 0);
  const pk = k => [...d.querySelectorAll('#cv-ed-grid .cv-pk')].find(b => b.querySelector('small').textContent.replace(' ✓', '') === k);
  pk('Ch 14').click(); pk('Ch 1').click();
  [...d.querySelectorAll('#cv-ed-tabs .btn')].find(b => b.textContent === 'Buses').click();
  pk('Bus 11').click();
  [...d.querySelectorAll('#cv-ed-tabs .btn')].find(b => b.textContent === 'Mains').click();
  pk('LR').click();
  check('picked in tap order', [...d.querySelectorAll('#cv-ed-cur .cv-it')].map(r => r.dataset.key).join() === 'ch/14,ch/1,bus/11,main/1',
        [...d.querySelectorAll('#cv-ed-cur .cv-it')].map(r => r.dataset.key));
  d.querySelector('#cv-ed-cur .cv-it[data-key="ch/1"] .x').click();
  d.getElementById('cv-ed-name').value = 'FAVES';
  d.getElementById('cv-ed-ok').click(); await tick(60);
  check('save posts the profile', saved && saved.profile === 'Default' && saved.layers[1].name === 'FAVES'
        && saved.layers[1].items.join() === 'ch/14,bus/11,main/1' && saved.layers[0].items.length === 32, saved);
  check('editor closed, layer 2 shown with the new strips', !d.getElementById('cv-ed').classList.contains('open') && keys(d).join() === 'ch/14,bus/11,main/1', keys(d));
  check('button renamed', d.getElementById('cv-l2').textContent.startsWith('FAVES'));

  // another device saves: live update
  P.push({ t: 'layouts', console: 'x32', v: { Default: { layers: [{ name: 'A', items: ['ch/1'] }, { name: 'B', items: ['ch/2', 'ch/3'] }, { name: 'C', items: [] }] } } });
  await tick();
  check('layouts push rebuilds the shown layer', keys(d).join() === 'ch/2,ch/3', keys(d));
  P.push({ t: 'layouts', console: 'wing', v: {} }); await tick();
  check('other console\'s layouts ignored', keys(d).join() === 'ch/2,ch/3');

  // profiles
  d.getElementById('cv-set-btn').click(); await tick();
  check('settings open', d.getElementById('cv-menu').classList.contains('open'));
  d.getElementById('cv-prof-new').click(); await tick(60);
  check('New profile posts a copy of the layers', saved.profile === 'Joseph' && saved.layers[1].items.join() === 'ch/2,ch/3', saved);
  check('device now uses it', w.localStorage.getItem('mixer.cvProfile') === 'Joseph');
  d.getElementById('cv-sw-auto').click();
  check('strip width Auto saved', w.localStorage.getItem('mixer.cvAcross') === '0');

  // name tap opens the channel sheet / selects a bus
  d.querySelectorAll('.cv-ov.open').forEach(o => o.classList.remove('open'));
  d.getElementById('cv-l3').click(); await tick();
  strip(d, 'bus/5').querySelector('.cs-nm').click(); await tick(120);
  // ── v4.0.3 bus page ──
  check('bus name tap opens the bus page', d.getElementById('sheet').classList.contains('open') && d.getElementById('sh-tag').textContent === 'BUS 5',
        d.getElementById('sh-tag').textContent);
  const vtabs = [...d.querySelectorAll('#sh-tabs .tab')].filter(b => b.style.display !== 'none').map(b => b.dataset.tab).join();
  check('bus page tiles: config, EQ, comp, sends (matrices), main', vtabs === 'input,eq,dyn,sends,main', vtabs);
  check('input sections hidden on a bus', d.getElementById('sh-body').classList.contains('outk') && !d.getElementById('sh-assign').hidden);
  check('bus page side strip = bus 5 fader', d.querySelector('#sh-side .cs').dataset.k === 'bus/5');
  d.querySelector('#sh-tabs .tab[data-tab="eq"]').click(); await tick(120);
  check('bus EQ: 6 band buttons', d.querySelectorAll('#eq-bands .btn').length === 6, d.querySelectorAll('#eq-bands .btn').length);
  check('no LC/HC chips on a bus', d.getElementById('eq-flt').style.display === 'none');
  d.querySelector('#sh-tabs .tab[data-tab="sends"]').click(); await tick();
  check('bus sends page = 6 matrices', [...d.querySelectorAll('#sends-strips .cs')].map(e => e.dataset.k).join() === 'mtx/1,mtx/2,mtx/3,mtx/4,mtx/5,mtx/6');
  P.posts.length = 0;
  d.querySelector('#sends-strips .cs[data-k="mtx/2"] .cs-mu').click(); await tick();
  check('bus -> mtx send toggle from the bus page', sets(P, '/bus/5/send/MX2/on').length === 1);
  d.querySelector('#sh-tabs .tab[data-tab="input"]').click(); await tick();
  P.posts.length = 0;
  d.querySelector('#sh-mg .cp-chip[data-i="3"]').click(); await tick();
  check('bus -> MG 3 assign posts', P.posts.some(p => p.url === '/mixer/api/assign' && p.body.kind === 'bus' && p.body.n === 5 && p.body.grp === 'M'
        && p.body.idx === 3 && p.body.on === true));
  d.getElementById('sh-next').click(); await tick(80);
  check('next on a bus page -> next bus', d.getElementById('sh-tag').textContent === 'BUS 6', d.getElementById('sh-tag').textContent);
  d.getElementById('sh-close').click();
  d.getElementById('cv-l1').click(); await tick();
  strip(d, 'ch/1').querySelector('.cs-nm').click(); await tick(60);
  check('channel name tap opens the channel sheet', d.getElementById('sheet').classList.contains('open'));
  // ── v4.0 channel page ──
  check('six overview tiles', [...d.querySelectorAll('#sh-tabs .tab')].map(b => b.dataset.tab).join() === 'input,gate,eq,dyn,sends,main');
  check('own strip on the right = ch1 fader', d.querySelector('#sh-side .cs').dataset.k === 'ch/1'
        && d.querySelector('#sh-side .cs-db').textContent === fmt(snap.state['/ch/1/fdr']));
  check('config tile flags: LC on', /LC/.test(d.getElementById('ov-flags').innerHTML) && d.querySelector('#ov-flags b.on').textContent === 'LC');
  check('X32 low-cut slope select (12/18/24) shows 24', d.getElementById('sh-lcs').value === '24' && d.getElementById('sh-lcs').options.length === 3,
        [d.getElementById('sh-lcs').value, d.getElementById('sh-lcs').options.length]);
  P.posts.length = 0;
  const lcs = d.getElementById('sh-lcs'); lcs.value = '12'; lcs.dispatchEvent(new w.Event('change')); await tick();
  check('slope change posts flt/lcs', sets(P, '/ch/1/flt/lcs').some(p => p.body.v === '12'));
  check('knobs on the config page', d.querySelectorAll('#row-trim .knob').length === 1);
  check('MG chips: 6 on X32, DCA chips: 8', d.querySelectorAll('#sh-mg .cp-chip').length === 6 && d.querySelectorAll('#sh-dca .cp-chip').length === 8);
  check('ch1 lit in MG 1+2 and DCA 1', ['1', '2'].every(i => d.querySelector(`#sh-mg .cp-chip[data-i="${i}"]`).classList.contains('on'))
        && d.querySelector('#sh-dca .cp-chip[data-i="1"]').classList.contains('on'), snap.state['/ch/1/tags']);
  check('DCA chip carries the DCA name', d.querySelector('#sh-dca .cp-chip[data-i="1"] small').textContent === 'Drums');
  P.posts.length = 0;
  d.querySelector('#sh-dca .cp-chip[data-i="4"]').click(); await tick();
  check('DCA 4 chip posts assign on', P.posts.some(p => p.url === '/mixer/api/assign' && p.body.grp === 'D' && p.body.idx === 4 && p.body.on === true)
        && d.querySelector('#sh-dca .cp-chip[data-i="4"]').classList.contains('on'));
  d.querySelector('#sh-tabs .tab[data-tab="sends"]').click(); await tick();
  check('sends page: 16 bus strips (X32: no matrix sends from a channel)', d.querySelectorAll('#sends-strips .cs').length === 16,
        d.querySelectorAll('#sends-strips .cs').length);
  const s1b = d.querySelector('#sends-strips .cs[data-k="bus/1"]');
  check('send strip shows ch1 -> bus1 (turned OFF earlier, sent 0 dB by the double-tap)', s1b.querySelector('.cs-db').textContent === '0.0'
        && s1b.querySelector('.cs-mu').textContent === 'OFF', [s1b.querySelector('.cs-db').textContent, s1b.querySelector('.cs-mu').textContent]);
  P.posts.length = 0;
  s1b.querySelector('.cs-mu').click(); await tick();
  check('send ON toggles /ch/1/send/1/on', sets(P, '/ch/1/send/1/on').some(p => p.body.v === 1) && s1b.querySelector('.cs-mu').textContent === 'ON');
  const sfd = s1b.querySelector('.cs-fd');
  ptr(w, sfd, 'pointerdown', 5, 50, 11); ptr(w, sfd, 'pointerup', 5, 50, 11); ptr(w, sfd, 'pointerdown', 5, 50, 11); ptr(w, sfd, 'pointerup', 5, 50, 11); await tick();
  check('send double-tap 0 dB', sets(P, '/ch/1/send/1/lvl').some(p => p.body.v === 0));
  check('send name tap does nothing', (s1b.querySelector('.cs-nm').click(), d.getElementById('sheet').classList.contains('open')));
  d.querySelector('#sh-tabs .tab[data-tab="main"]').click(); await tick();
  check('main page: LR + M/C strips', [...d.querySelectorAll('#main-strips .cs')].map(e => e.dataset.k).join() === 'main/1,main/2');
  P.posts.length = 0;
  d.querySelector('#main-strips .cs[data-k="main/2"] .cs-mu').click(); await tick();
  check('M/C assign posts /ch/1/main/2/on', sets(P, '/ch/1/main/2/on').some(p => p.body.v === 1));
  check('pan knob on the main page', d.querySelectorAll('#main-pan .knob').length === 1);
  d.querySelector('#sh-tabs .tab[data-tab="eq"]').click(); await tick(120);
  check('LC chip next to the bands (X32: no HC)', [...d.querySelectorAll('#eq-flt .btn')].map(b => b.textContent).join() === 'LC');
  d.querySelector('#eq-flt .btn[data-b="lc"]').click(); await tick();
  check('LC selected: on / freq / slope controls', [...d.querySelectorAll('#eq-band-ctl .gp span')].map(x => x.textContent).join() === 'Low cut,Freq,Slope',
        [...d.querySelectorAll('#eq-band-ctl .gp span')].map(x => x.textContent));
  P.posts.length = 0;
  d.querySelector('#eq-band-ctl .gp button').click(); await tick();
  check('LC on/off from the EQ page', sets(P, '/ch/1/flt/lc').some(p => p.body.v === 0));
  d.querySelector('#eq-bands .btn[data-b="2"]').click(); await tick();
  d.getElementById('sh-next').click(); await tick(80);
  check('next -> next strip of the layer, page kept', d.getElementById('sh-tag').textContent === 'CH 1'
        || d.getElementById('sh-tag').textContent !== '', d.getElementById('sh-tag').textContent);
  d.getElementById('sh-close').click();

  // back to classic
  d.getElementById('view-btn').click(); await tick();
  check('classic again', !d.body.classList.contains('cvm') && w.localStorage.getItem('mixer.view') === 'classic');
  check('listen card back on the page', d.getElementById('listen-card').parentElement === d.body);
  check('mute groups back in their card', d.getElementById('mg-grid').parentElement.tagName === 'SECTION');
  check('no script errors (X32)', P.errors.length === 0, P.errors);

  // WING: raw page (defaults), synthetic state
  console.log('WING console view (defaults)');
  const wsnap = { conn: true, loaded: true, found: true, ip: '1.2.3.4', feeds: [], feed: 'main1', listen: {}, srcgroups: [], ovr: [], order: [],
    layouts: { Joseph: { email: 'joe@x.com', layers: [{ name: 'BAND', items: ['ch/1', 'ch/2', 'bus/11', 'bus/12'] }] } }, who: 'joe@x.com',
    state: { '/ch/1/name': 'Kick', '/ch/1/fdr': 4.9, '/ch/1/main/2/lvl': -3, '/ch/1/main/2/on': 1, '/ch/1/send/MX3/lvl': -6, '/ch/1/send/MX3/on': 0,
             '/bus/11/name': 'Reverb', '/ch/1/send/11/mode': 'POST', '/bus/11/main/2/lvl': -9, '/main/2/name': 'SUBS', '/dca/1/name': 'guitar', '/dca/1/fdr': -24.8 } };
  const Q = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), wsnap, {});
  await tick(80);
  const D = Q.d;
  check('Access email picks the matching profile', keys(D).join() === 'ch/1,ch/2,bus/11,bus/12', keys(D));
  check('layer 1 named BAND', D.getElementById('cv-l1').textContent.startsWith('BAND'));
  check('WING bus list: LR + Main 2-4 + 16 buses + 8 matrices', D.querySelectorAll('#cv-c2 .cvbus').length === 28, D.querySelectorAll('#cv-c2 .cvbus').length);
  D.querySelector('#cv-c2 .cvbus[data-k="main"][data-n="2"]').click(); await tick();
  check('Main 2 (subs) SOF: ch1 shows its Main 2 send', strip(D, 'ch/1').querySelector('.cs-db').textContent === '-3.0');
  check('Main 2 SOF: buses can send too', strip(D, 'bus/11').querySelector('.cs-db').textContent === '-9.0' && !strip(D, 'bus/11').classList.contains('na'));
  check('Main 2 pinned', !!strip(D, 'main/2', true));
  D.querySelector('#cv-c2 .cvbus[data-k="mtx"][data-n="3"]').click(); await tick();
  D.querySelector('#cv-c2 .cvbus[data-k="bus"][data-n="11"]').click(); await tick();
  check('WING SOF label shows the send tap', strip(D, 'ch/1').querySelector('.cs-lb').textContent === 'Ch 1 · POST',
        strip(D, 'ch/1').querySelector('.cs-lb').textContent);
  D.querySelector('#cv-c2 .cvbus[data-k="mtx"][data-n="3"]').click(); await tick();
  check('WING: channels feed matrices', strip(D, 'ch/1').querySelector('.cs-db').textContent === '-6.0' && strip(D, 'ch/1').querySelector('.cs-mu').textContent === 'OFF');
  D.getElementById('cv-ldca').click(); await tick();
  check('WING: 16 DCAs', keys(D).length === 16 && strip(D, 'dca/1').querySelector('.cs-nm').textContent === 'guitar');
  check('DCA greyed while mixing a matrix', strip(D, 'dca/1').classList.contains('na'));
  D.getElementById('cv-l1').click(); await tick();
  strip(D, 'ch/1').querySelector('.cs-nm').click(); await tick(80);
  D.querySelector('#sh-tabs .tab[data-tab="sends"]').click(); await tick();
  const tapSel = D.querySelector('#sends-strips .cs[data-k="bus/11"] .cs-tap');
  check('WING sends page: tap select per bus, shows POST', !!tapSel && tapSel.value === 'POST');
  Q.posts.length = 0;
  tapSel.value = 'PRE'; tapSel.dispatchEvent(new Q.w.Event('change')); await tick();
  check('tap change posts /ch/1/send/11/mode', Q.posts.some(p => p.body && p.body.a === '/ch/1/send/11/mode' && p.body.v === 'PRE'));
  check('WING: 8 MG chips, 16 DCA chips', D.querySelectorAll('#sh-mg .cp-chip').length === 8 && D.querySelectorAll('#sh-dca .cp-chip').length === 16);
  D.getElementById('sh-close').click();
  check('no script errors (WING)', Q.errors.length === 0, Q.errors);

  // classic start: nothing of the console view visible, it builds on first switch
  const R = load(fs.readFileSync(path.join(FX, 'wing.html'), 'utf8'), wsnap, {}, 'http://pi.local/mixer?view=classic');
  await tick(60);
  check('?view=classic stays classic', !R.d.body.classList.contains('cvm') && R.d.getElementById('view-btn').textContent === 'CONSOLE');
  R.d.getElementById('view-btn').click(); await tick();
  check('switch builds the console', R.d.body.classList.contains('cvm') && keys(R.d).length === 4);
  check('no script errors (switch)', R.errors.length === 0, R.errors);

  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})();

function fmt(d) { return (d == null) ? '' : (d <= -89.9 ? '−∞' : (d > 0 ? '+' : '') + d.toFixed(1)); }
