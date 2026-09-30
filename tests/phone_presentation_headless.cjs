/* Run with NODE_PATH=<bundled node_modules> node tests/phone_presentation_headless.cjs.
 * Uses headless Edge and a fake socket; no desktop window, audio or live agent. */
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..', 'web');
const server = http.createServer((req, res) => {
  const pathname = req.url.split('?')[0];
  const name = pathname === '/' ? 'index.html' : pathname.replace(/^\/static\//, '');
  const file = path.join(root, name);
  if (!file.startsWith(root) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});

async function run() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  const page = await browser.newPage({ viewport: { width: 900, height: 520 }, hasTouch: true });
  page.on('pageerror', e => console.error('page error:', e));
  await page.addInitScript(() => {
    class FakeSocket {
      constructor() { this.readyState = 1; this.sent = []; window.mockSocket = this; setTimeout(() => this.onopen && this.onopen(), 0); }
      send(s) { this.sent.push(JSON.parse(s)); }
      emit(m) { this.onmessage({ data: JSON.stringify(m) }); }
      close() { this.readyState = 3; }
    }
    window.WebSocket = FakeSocket;
    window.navigator.serviceWorker = undefined;
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => !!window.SC && !!window.mockSocket);
  const clip = id => ({ id, label: id, emoji: '♪', gain: 1, voice: true, ears: true, duration: .1 });
  const initial = { revision: 0, appOrder: [], hiddenApps: [], pages: { soundboard: 'top', devices: 'right', media: 'bottom' }, padSlots: ['a', 'b', ...Array(10).fill(null)] };
  const snap = { t: 'snapshot', presentation: initial, sessions: [{ id: 'led', appLabel: 'LED Keeper 2', level: .5 }, { id: 'shell', appLabel: 'Shell Experience Host', level: .4 }], devices: {}, soundboard: { clips: [clip('a'), clip('b'), clip('c')], runtime: 'ready' }, media: [] };
  await page.evaluate(s => mockSocket.emit(s), snap);
  assert.strictEqual(await page.locator('#soundboard-pads > button').count(), 12);
  assert.strictEqual(await page.locator('#apps-grid .tile').count(), 2);
  const verifyDir = path.resolve(__dirname, '..', 'release', 'verify-v0.6.1');
  fs.mkdirSync(verifyDir, { recursive: true });
  for (const size of [{ width: 800, height: 360, name: 'landscape' }, { width: 393, height: 800, name: 'portrait' }]) {
    await page.setViewportSize(size);
    await page.waitForTimeout(320);
    await page.screenshot({ path: path.join(verifyDir, `phone-mixer-${size.name}.png`) });
    await page.click('#tab-soundboard');
    await page.waitForTimeout(350);
    await page.screenshot({ path: path.join(verifyDir, `phone-soundboard-${size.name}.png`) });
    await page.click('#tab-mixer');
    await page.waitForTimeout(350);
  }
  await page.setViewportSize({ width: 800, height: 360 });

  // Rapid repeated taps play immediately and leave no editor timer behind.
  await page.click('#tab-soundboard');
  const pad = page.locator('#soundboard-pads > button').first();
  for (let i = 0; i < 4; i++) await pad.click();
  await page.waitForTimeout(1650);
  assert.strictEqual(await page.locator('#soundboard-editor:not(.hidden)').count(), 0);
  assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'soundboard_play').length), 4);
  await pad.hover(); await page.mouse.down(); await page.waitForTimeout(1600); await page.mouse.up();
  assert.strictEqual(await page.locator('#soundboard-editor:not(.hidden)').count(), 1);
  const saveVisible = await page.locator('#soundboard-edit-save').evaluate(node => {
    const r = node.getBoundingClientRect();
    const bg = getComputedStyle(node).backgroundColor;
    return r.top >= 0 && r.bottom <= innerHeight && r.width >= 40 && bg === 'rgb(179, 140, 255)';
  });
  assert(saveVisible, 'Save button must fit in landscape viewport');
  await page.screenshot({ path: path.join(verifyDir, 'phone-soundboard-editor-landscape.png') });
  await page.setViewportSize({ width: 393, height: 800 });
  assert(await page.locator('#soundboard-edit-save').evaluate(node => node.getBoundingClientRect().bottom <= innerHeight), 'Save button must fit in portrait viewport');
  await page.screenshot({ path: path.join(verifyDir, 'phone-soundboard-editor-portrait.png') });
  await page.setViewportSize({ width: 800, height: 360 });
  await page.fill('#soundboard-edit-label', 'New A');
  await page.locator('#soundboard-edit-gain').evaluate(node => { node.value = '0.55'; node.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.uncheck('#soundboard-edit-voice');
  await page.click('#soundboard-edit-save');
  const edit = await page.evaluate(() => mockSocket.sent.at(-1));
  assert.strictEqual(edit.t, 'soundboard_update_clip');
  assert.strictEqual(edit.changes.label, 'New A');
  assert.strictEqual(edit.changes.gain, .55);
  assert.strictEqual(edit.changes.voice, false);
  assert.strictEqual(await page.locator('#soundboard-edit-save').textContent(), 'Saving…');
  await page.evaluate(changes => mockSocket.emit({ t: 'soundboard', soundboard: { clips: [{ ...SC.model.soundboard.clips[0], ...changes }, ...SC.model.soundboard.clips.slice(1)], runtime: 'ready' } }), edit.changes);
  assert.strictEqual(await page.locator('#soundboard-editor:not(.hidden)').count(), 0);
  assert(await page.locator('#toast.toast-success').evaluate(node => {
    const icon = node.querySelector('.t-ico');
    return getComputedStyle(icon).backgroundColor === 'rgb(36, 76, 50)' && node.getBoundingClientRect().bottom < document.querySelector('.sound-pad-emoji').getBoundingClientRect().top;
  }), 'saved toast must be green and clear of pad icons');
  await pad.hover(); await page.mouse.down(); await page.waitForTimeout(1600); await page.mouse.up();
  assert.strictEqual(await page.inputValue('#soundboard-edit-label'), 'New A');
  assert.strictEqual(await page.inputValue('#soundboard-edit-gain'), '0.55');
  await page.click('#soundboard-edit-delete');
  const removed = await page.evaluate(() => mockSocket.sent.at(-1));
  assert.strictEqual(removed.t, 'presentation_update');
  assert.strictEqual(removed.changes.padSlots[0], null);
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 1, padSlots: [null, 'b', ...Array(10).fill(null)] } }));
  await page.locator('#soundboard-pads > button').first().click();
  assert.strictEqual(await page.locator('#soundboard-choices button').count(), 2);
  await page.locator('#soundboard-choices button').last().click();
  const replaced = await page.evaluate(() => mockSocket.sent.at(-1));
  assert.strictEqual(replaced.changes.padSlots[0], 'c');

  // A held finger tracks the neighbor; repeated snapshots at the same revision
  // must not snap the page back. Reverse motion cannot commit the initial target.
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 2, padSlots: ['c', 'b', ...Array(10).fill(null)] } }));
  await page.click('#tab-mixer');
  const gesture = async (from, moves, snapshotMid = false) => page.evaluate(({ from, moves, snapshotMid, snap }) => {
    const selectors = { mixer: '.zone-label', devices: '.dev-head', media: '.media-topbar', soundboard: '.soundboard-title' };
    const node = document.querySelector(selectors[SC.model.activePage]);
    const fire = (type, p) => {
      const touch = new Touch({ identifier: 1, target: node, clientX: p[0], clientY: p[1] });
      node.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true, touches: type === 'touchend' ? [] : [touch], changedTouches: [touch] }));
    };
    fire('touchstart', from);
    moves.forEach((p, i) => {
      fire('touchmove', p);
      if (i === 0 && snapshotMid) {
        const before = document.querySelector('.page-mixer').style.transform;
        mockSocket.emit({ ...snap, presentation: SC.model.presentation });
        const after = document.querySelector('.page-mixer').style.transform;
        if (before !== after) throw new Error('snapshot reset finger-tracked transition');
      }
    });
    fire('touchend', moves.at(-1));
    return SC.model.activePage;
  }, { from, moves, snapshotMid, snap });
  assert.strictEqual(await gesture([400, 170], [[280, 170], [180, 170]], true), 'devices');
  await page.click('#dev-back');
  assert.strictEqual(await gesture([400, 80], [[400, 180], [400, 250]]), 'soundboard');
  await page.click('#tab-mixer');
  assert.strictEqual(await gesture([400, 250], [[400, 145], [400, 70]]), 'media');
  await page.click('#tab-mixer');
  assert.strictEqual(await gesture([400, 170], [[350, 170], [500, 170]]), 'mixer');

  // Reconfigure every occupied direction and verify the opposite swipe returns.
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 3, pages: { soundboard: 'left', devices: 'bottom', media: 'top' } } }));
  assert.strictEqual(await gesture([400, 170], [[510, 170], [590, 170]]), 'soundboard');
  assert.strictEqual(await gesture([400, 170], [[290, 170], [210, 170]]), 'mixer');
  assert.strictEqual(await gesture([400, 80], [[400, 180], [400, 250]]), 'media');
  assert.strictEqual(await gesture([400, 250], [[400, 150], [400, 70]]), 'mixer');
  assert.strictEqual(await gesture([400, 250], [[400, 150], [400, 70]]), 'devices');
  assert.strictEqual(await gesture([400, 80], [[400, 180], [400, 250]]), 'mixer');

  // Actual browser touch dispatch, starting inside pads (not a blank header).
  const cdp = await page.context().newCDPSession(page);
  for (const [i, side] of ['top', 'bottom', 'left', 'right'].entries()) {
    const remaining = ['top', 'bottom', 'left', 'right'].filter(x => x !== side);
    await page.evaluate(({ revision, side, remaining }) => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision, pages: { soundboard: side, devices: remaining[0], media: remaining[1] } } }), { revision: 10 + i, side, remaining });
    await page.click('#tab-soundboard');
    await page.waitForTimeout(320);
    const bounds = await page.locator('#soundboard-pads > button').nth(i % 2 ? 5 : 1).boundingBox();
    const start = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
    const delta = { top: [0, -75], bottom: [0, 75], left: [-100, 0], right: [100, 0] }[side];
    const playsBefore = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'soundboard_play').length);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [start] });
    for (const portion of [.25, .5, .75, 1]) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: start.x + delta[0] * portion, y: start.y + delta[1] * portion }] });
      await page.waitForTimeout(20);
    }
    await page.evaluate(s => mockSocket.emit({ ...s, presentation: SC.model.presentation }), snap);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(320);
    assert.strictEqual(await page.evaluate(() => SC.model.activePage), 'mixer', `pad-origin return from ${side}`);
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'soundboard_play').length), playsBefore);
    assert.strictEqual(await page.locator('#soundboard-chooser:not(.hidden)').count(), 0);
    assert.strictEqual(await page.locator('.page-mixer').evaluate(node => Math.round(node.getBoundingClientRect().left)), 0);
  }

  // A poll during a lifted tile must keep its DOM and save the drop order.
  await page.waitForTimeout(350);
  await page.mouse.move(170, 130); await page.mouse.down(); await page.waitForTimeout(430);
  await page.mouse.move(390, 130, { steps: 5 });
  await page.evaluate(s => mockSocket.emit({ ...s, presentation: SC.model.presentation }), snap);
  await page.mouse.up();
  const reordered = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'presentation_update').at(-1));
  assert.strictEqual(reordered.t, 'presentation_update');
  assert.deepStrictEqual(reordered.changes.appOrder, ['shell', 'led']);

  // Hidden IDs stay in their slots when the remaining visible apps are dragged.
  const three = { ...snap, sessions: [...snap.sessions, { id: 'new', appLabel: 'New App', level: .2 }] };
  await page.evaluate(s => mockSocket.emit({ ...s, presentation: { ...SC.model.presentation, revision: 14, appOrder: ['shell', 'led', 'new'], hiddenApps: ['led'] } }), three);
  assert.strictEqual(await page.locator('#apps-grid .tile').count(), 2);
  await page.mouse.move(170, 130); await page.mouse.down(); await page.waitForTimeout(430);
  await page.mouse.move(390, 130, { steps: 5 });
  await page.evaluate(s => mockSocket.emit({ ...s, presentation: SC.model.presentation }), three);
  await page.mouse.up();
  const hiddenReorder = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'presentation_update').at(-1));
  assert.deepStrictEqual(hiddenReorder.changes.appOrder, ['new', 'led', 'shell']);
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 15, appOrder: ['new', 'led', 'shell'] } }));

  // Holding a tile exposes Hide; hiding changes presentation only.
  await page.locator('[data-app-id="shell"]').hover({ position: { x: 25, y: 20 } }); await page.mouse.down(); await page.waitForTimeout(430); await page.mouse.up();
  assert.strictEqual(await page.locator('#tile-hide-drop').count(), 1);
  await page.click('#tile-hide-drop');
  const hide = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'presentation_update').at(-1));
  assert.deepStrictEqual(hide.changes.hiddenApps, ['led', 'shell']);
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 16, hiddenApps: ['led', 'shell'] } }));
  assert.strictEqual(await page.locator('#apps-grid .tile').count(), 1);
  assert.strictEqual(await page.evaluate(() => SC.model.apps.length), 3);
  await page.evaluate(() => mockSocket.emit({ t: 'presentation', presentation: { ...SC.model.presentation, revision: 17, hiddenApps: ['led', 'shell', 'new'] } }));
  assert.strictEqual(await page.locator('#apps-grid .tile').count(), 0);
  assert.strictEqual(await page.evaluate(() => SC.model.selectedId), null);
  assert.strictEqual(await page.evaluate(() => SC.model.dialMode), 'system');
  // Real touches on Media content at every outer placement, including both sides.
  async function touchSwipe(selector, delta) {
    await page.waitForTimeout(320);
    const b = await page.locator(selector).first().boundingBox();
    const start = { x: b.x+b.width/2, y: b.y+b.height/2 };
    await cdp.send('Input.dispatchTouchEvent', {type:'touchStart',touchPoints:[start]});
    for(const p of [.25,.5,.75,1]) await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x+delta[0]*p,y:start.y+delta[1]*p}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await page.waitForTimeout(320);
  }
  const returnDelta={top:[0,-85],bottom:[0,85],left:[-130,0],right:[130,0]};
  for(const [i,side] of ['top','bottom','left','right'].entries()) {
    const others=['top','bottom','left','right'].filter(p=>p!==side);
    await page.evaluate(({snap,side,others,i})=>mockSocket.emit({...snap,presentation:{...SC.model.presentation,revision:30+i,hiddenApps:[],pages:{media:side,soundboard:others[0],devices:others[1]}}}),{snap,side,others,i});
    await page.click('#tab-mixer');
    const go=returnDelta[side].map(v=>-v);
    assert.strictEqual(await gesture([400,170],[[400+go[0],170+go[1]]]),'media',`Open Media at ${side}`);
    await touchSwipe('.media-list',returnDelta[side]);
    assert.strictEqual(await page.evaluate(()=>SC.model.activePage),'mixer',`Media content return from ${side}`);
  }
  // Mixer can be an outer page with Soundboard at the center.
  for(const [i,side] of ['top','bottom','left','right'].entries()) {
    const others=['top','bottom','left','right'].filter(p=>p!==side);
    await page.evaluate(({snap,side,others,i})=>mockSocket.emit({...snap,presentation:{...SC.model.presentation,revision:40+i,pages:{mixer:side,soundboard:'center',devices:others[0],media:others[1]}}}),{snap,side,others,i});
    await page.click('#tab-mixer');
    const d=returnDelta[side];
    assert.strictEqual(await gesture([400,170],[[400+d[0],170+d[1]]]),'soundboard');
  }
  // The long hold claims a real touch gesture before any page swipe can begin.
  await page.evaluate(s=>mockSocket.emit({...s,presentation:{...SC.model.presentation,revision:50,hiddenApps:[],pages:{media:'left',soundboard:'top',devices:'right'}}}),snap);
  await page.click('#tab-mixer');await page.waitForTimeout(350);
  const tile=await page.locator('#apps-grid .tile').first().boundingBox();
  const hold={x:tile.x+45,y:tile.y+25};
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[hold]});await page.waitForTimeout(450);
  assert.strictEqual(await page.locator('.tile-held').count(),1);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:hold.x+130,y:hold.y}]});
  await page.evaluate(s=>mockSocket.emit({...s,presentation:SC.model.presentation}),snap);
  assert.strictEqual(await page.evaluate(()=>SC.model.activePage),'mixer');
  assert.strictEqual(await page.locator('.tile-drag-clone').count(),1);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]});
  assert.strictEqual(await page.locator('.tile-drag-clone').count(),0);
  await browser.close();
  console.log('phone presentation headless checks passed');
}
run().catch(e => { console.error(e); process.exitCode = 1; }).finally(() => server.close());
