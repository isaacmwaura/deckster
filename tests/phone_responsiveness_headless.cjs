/* Run with NODE_PATH=<bundled node_modules> node tests/phone_responsiveness_headless.cjs.
 * Exercises delayed PC updates and interrupted real touches without live audio. */
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..', 'web');
const server = http.createServer((req, res) => {
  const name = req.url.split('?')[0] === '/' ? 'index.html' : req.url.split('?')[0].replace(/^\/static\//, '');
  const file = path.join(root, name);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});

async function run() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 520 }, hasTouch: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.sockets = [];
      class FakeSocket {
        constructor() { this.readyState = 1; this.sent = []; sockets.push(this); window.mockSocket = this; setTimeout(() => this.onopen && this.onopen(), 0); }
        send(value) { this.sent.push(JSON.parse(value)); }
        emit(value) { this.onmessage({ data: JSON.stringify(value) }); }
        close() { this.readyState = 3; if (this.onclose) this.onclose(); }
      }
      window.WebSocket = FakeSocket;
      window.navigator.serviceWorker = undefined;
      HTMLElement.prototype.requestFullscreen = () => Promise.resolve();
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => !!window.SC && !!window.mockSocket);
    const snapshot = {
      t: 'snapshot', presentation: { revision: 1, appOrder: [], hiddenApps: [], pages: { soundboard: 'top', devices: 'right', media: 'bottom' }, padSlots: ['tone', ...Array(11).fill(null)] },
      sessions: Array.from({ length: 30 }, (_, index) => ({ id: `app${index}`, appLabel: `App ${index}`, level: .5, muted: false })),
      devices: { outputs: [{ id: 'out', name: 'Speakers', isDefault: true }], inputs: [{ id: 'in', name: 'Microphone', isDefault: true }], meters: { output: .4, input: .2 } },
      media: [{ id: 'media', app: 'Chrome', title: 'Video', status: 'playing', thumbKey: 'video', canNext: true }, { id: 'music', app: 'Spotify', title: 'Music', status: 'paused' }],
      soundboard: { runtime: 'ready', clips: [{ id: 'tone', label: 'Tone', emoji: '♪', gain: 1, voice: true, ears: true, duration: 1 }] }
    };
    await page.evaluate(value => mockSocket.emit(value), snapshot);

    // A busy PC's meter polls retain the interactive nodes and loaded artwork.
    const workload = await page.evaluate(value => {
      const nodes = ['#apps-grid > button', '#outputs > button', '#media-list > div', '#soundboard-pads > button', '#dial-host svg > path'].map(selector => document.querySelector(selector));
      let mutations = 0;
      const observer = new MutationObserver(records => { mutations += records.filter(record => record.type === 'childList').length; });
      observer.observe(document.querySelector('#app'), { childList: true, subtree: true });
      const started = performance.now();
      for (let index = 0; index < 100; index++) mockSocket.emit({ ...value, devices: { ...value.devices, meters: { output: index / 100, input: .1 } } });
      const elapsed = performance.now() - started;
      const preserved = nodes.every(node => document.contains(node));
      mutations += observer.takeRecords().filter(record => record.type === 'childList').length;
      observer.disconnect();
      return { preserved, mutations, elapsed };
    }, snapshot);
    assert(workload.preserved, 'meter snapshots must retain tiles, device buttons, media cards, pads and dial paths');
    assert.strictEqual(workload.mutations, 0, 'unchanged control trees must not be recreated for meter polls');

    // Reused mute controls must use the current snapshot, not a captured old object.
    await page.evaluate(value => mockSocket.emit({ ...value, sessions: value.sessions.map(session => ({ ...session, muted: session.id === 'app0' })) }), snapshot);
    await page.locator('[data-app-id="app0"] .chip').first().click();
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(message => message.t === 'set_mute').at(-1).muted), false);

    const cdp = await page.context().newCDPSession(page);
    const b = await page.locator('#dial-host').boundingBox();
    const start = { x: b.x + b.width * .3, y: b.y + b.height * .6 };
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [start] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: start.x, y: start.y - 35 }] });
    assert.strictEqual(await page.evaluate(() => SC.dial.dragging), true);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
    assert.strictEqual(await page.evaluate(() => SC.dial.dragging), false, 'Android cancellation must end the dial gesture');
    const afterCancel = await page.evaluate(() => ({ value: SC.dial.val, sent: mockSocket.sent.filter(message => message.t === 'set_volume').length }));
    await page.evaluate(() => {
      const node = document.querySelector('.zone-label');
      node.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true, pointerId: 8, pointerType: 'touch', buttons: 1, clientX: 400, clientY: 30 }));
    });
    await page.waitForTimeout(90);
    assert.deepStrictEqual(await page.evaluate(() => ({ value: SC.dial.val, sent: mockSocket.sent.filter(message => message.t === 'set_volume').length })), afterCancel, 'an unrelated later finger must not move or command the dial');

    // Ownership checks ignore another touch and ordinary taps issue no command.
    const ownership = await page.evaluate(() => {
      const node = document.querySelector('#dial-host svg');
      const r = node.getBoundingClientRect();
      function fire(type, id, dy = 0) {
        node.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: id, pointerType: 'touch', isPrimary: true, button: 0, buttons: type === 'pointerup' ? 0 : 1, clientX: r.left + 30, clientY: r.top + r.height / 2 + dy }));
      }
      const value = SC.dial.val, before = mockSocket.sent.filter(message => message.t === 'set_volume').length;
      fire('pointerdown', 20); fire('pointermove', 21, -80); fire('pointerup', 21);
      const stillOwned = SC.dial.dragging;
      fire('pointerup', 20);
      return { value, after: SC.dial.val, stillOwned, before, sent: mockSocket.sent.filter(message => message.t === 'set_volume').length };
    });
    assert(ownership.stillOwned, 'another finger must not end the owned gesture');
    assert.strictEqual(ownership.after, ownership.value);
    assert.strictEqual(ownership.sent, ownership.before, 'touch without dial movement must not command volume');

    // Stale PC echoes cannot rewind a local adjustment, including after release.
    await page.waitForTimeout(1550);
    await page.evaluate(value => mockSocket.emit(value), snapshot);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 50, 'reset from the authoritative snapshot');
    await page.evaluate(() => { SC.dial.nudge(1); mockSocket.emit({ t: 'state', target: { kind: 'session', id: 'app0' }, level: .5 }); });
    await page.evaluate(value => mockSocket.emit(value), snapshot);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 51);
    await page.evaluate(() => { for (let index = 0; index < 9; index++) SC.dial.nudge(1); });
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(message => message.t === 'set_volume').at(-1).level), .6, 'rapid +/- taps must deliver the final value');
    await page.waitForTimeout(1550);
    await page.evaluate(() => mockSocket.emit({ t: 'state', target: { kind: 'session', id: 'app0' }, level: .4 }));
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 40, 'external PC changes resume after the acknowledgement guard');

    // A trailing drag command belongs to its original target and is canceled
    // before a mode switch or socket outage can send it to the wrong control.
    await page.evaluate(() => {
      const node = document.querySelector('#dial-host svg'), r = node.getBoundingClientRect();
      function fire(type, y) {
        node.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 30, pointerType: 'touch', isPrimary: true, button: 0, buttons: 1, clientX: r.left + 20, clientY: y }));
      }
      fire('pointerdown', r.top + 140); fire('pointermove', r.top + 130); fire('pointermove', r.top + 110);
      document.querySelector('[data-mode="system"]').click();
      window.sentAtSwitch = mockSocket.sent.filter(message => message.t === 'set_volume').length;
    });
    await page.waitForTimeout(90);
    assert.strictEqual(await page.evaluate(() => SC.dial.dragging), false);
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(message => message.t === 'set_volume').length), await page.evaluate(() => sentAtSwitch));

    // Returning to the app during backoff must not open a second socket later.
    await page.evaluate(() => { mockSocket.close(); document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForTimeout(650);
    assert.strictEqual(await page.evaluate(() => sockets.length), 2);
    await page.evaluate(value => { sockets[0].emit({ ...value, sessions: [] }); mockSocket.emit(value); }, snapshot);
    assert.strictEqual(await page.locator('#apps-grid > button').count(), 30);

    // The legacy touch fallback must also receive cancellation when redraws
    // detach the path that Android originally chose as the touch target.
    await page.addInitScript(() => { window.PointerEvent = undefined; });
    await page.reload();
    await page.waitForFunction(() => !!window.SC && !!window.mockSocket);
    await page.evaluate(value => mockSocket.emit(value), snapshot);
    const legacy = await page.locator('#dial-host').boundingBox();
    const legacyStart = { x: legacy.x + legacy.width * .3, y: legacy.y + legacy.height * .6 };
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [legacyStart] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: legacyStart.x, y: legacyStart.y - 35 }] });
    assert.strictEqual(await page.evaluate(() => SC.dial.dragging), true);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
    assert.strictEqual(await page.evaluate(() => SC.dial.dragging), false, 'legacy canceled touch must also clean up after SVG redraw');
    assert.deepStrictEqual(errors, []);
    console.log(`phone responsiveness checks passed; 100 snapshots ${Math.round(workload.elapsed)} ms, ${workload.mutations} control-tree mutations`);
  } finally { await browser.close(); }
}
run().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
