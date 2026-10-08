/* Identified control results, delayed observations and suspended phone rendering. */
const assert = require('assert'), fs = require('fs'), http = require('http'), path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../web');
const server = http.createServer((req, res) => {
  const name = req.url.split('?')[0] === '/' ? 'index.html' : req.url.split('?')[0].replace(/^\/static\//, '');
  const file = path.join(root, name);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
const snapshot = (ownerSeq = 10, level = .5, serverEpoch = 'pc-one') => ({
  t: 'snapshot', serverEpoch, capabilities: ['command-results', 'owner-sequence'],
  sessions: [{ id: 'app', appLabel: 'App', level, muted: false, ownerSeq }],
  devices: { speakerMaster: { level: .6, muted: false, ownerSeq }, micMaster: { level: .7, muted: false, ownerSeq },
    outputs: [{ id: 'out', name: 'Speakers', isDefault: true }], inputs: [{ id: 'in', name: 'Mic', isDefault: true }], meters: { output: .4, input: .2 } },
  presentation: { revision: 1, appOrder: [], hiddenApps: [], pages: { soundboard: 'top', devices: 'right', media: 'bottom' }, padSlots: [] },
  soundboard: { runtime: 'ready', clips: [] }, media: []
});
async function run() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 520 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.sockets = []; window.meterFrames = 0;
      const raf = window.requestAnimationFrame;
      window.requestAnimationFrame = fn => raf.call(window, time => { if (fn.name === 'meterFrame') meterFrames++; fn(time); });
      const timeout = window.setTimeout;
      window.setTimeout = (fn, ms, ...args) => { if (ms === 120000) window.startSaver = fn; return timeout(fn, ms, ...args); };
      window.AndroidBridge = {
        getPowerMode: () => localStorage.getItem('native_power') || 'mounted',
        setPowerMode: value => localStorage.setItem('native_power', value),
        getDeviceId: () => 'test-phone', getToken: () => '', getDeviceName: () => 'Test phone'
      };
      window.WebSocket = class {
        constructor() { this.readyState = 1; this.sent = []; sockets.push(this); window.mockSocket = this; timeout(() => this.onopen && this.onopen(), 0); }
        send(raw) { this.sent.push(JSON.parse(raw)); }
        emit(value) { this.onmessage({ data: JSON.stringify(value) }); }
        close() { this.readyState = 3; if (this.onclose) this.onclose(); }
      };
      HTMLElement.prototype.requestFullscreen = () => Promise.resolve();
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => window.SC && window.mockSocket);
    await page.evaluate(value => mockSocket.emit(value), snapshot());
    await page.evaluate(() => SC.dial.nudge(1));
    const first = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').at(-1));
    assert(first.commandId && first.clientSeq > 0, 'controls must carry command identity and sequence');
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'accepted' }), first);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 51, 'accepted admission does not confirm a native write');
    // A slow native write outlives the previous 1.5-second optimism heuristic.
    await page.waitForTimeout(1700);
    await page.evaluate(value => mockSocket.emit(value), snapshot(9));
    await page.evaluate(() => mockSocket.emit({ t: 'state', serverEpoch: 'pc-one', target: { kind: 'session', id: 'app' }, ownerSeq: 8, level: .4, muted: false }));
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 51);
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'applied', ownerSeq: 11, observation: { level: .51, muted: false } }), first);
    await page.evaluate(() => mockSocket.emit({ t: 'state', serverEpoch: 'pc-one', target: { kind: 'session', id: 'app' }, ownerSeq: 10, level: .2, muted: false }));
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 51, 'an older poll cannot rewind an applied write');
    await page.evaluate(() => mockSocket.emit({ t: 'state', serverEpoch: 'pc-one', target: { kind: 'session', id: 'app' }, ownerSeq: 12, level: .31, muted: false }));
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 31, 'a newer external change wins immediately');
    await page.evaluate(() => { SC.dial.nudge(1); SC.dial.nudge(1); });
    const [older, latest] = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').slice(-2));
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'applied', ownerSeq: 13, observation: { level: .32, muted: false } }), older);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 33, 'an older result cannot clear the latest intent');
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'failed', error: 'Endpoint disappeared' }), latest);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 32, 'failed current intent returns to the newest observation');
    await page.locator('[data-app-id="app"] .chip').first().click();
    const mute = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_mute').at(-1));
    await page.evaluate(value => mockSocket.emit(value), snapshot(10, .5));
    assert.strictEqual(await page.evaluate(() => SC.model.apps[0].muted), true);
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'applied', ownerSeq: 14, observation: { level: .32, muted: true } }), mute);
    await page.evaluate(value => mockSocket.emit(value), snapshot(10));
    assert.strictEqual(await page.evaluate(() => SC.model.apps[0].muted), true, 'stale snapshots cannot undo applied mute');
    // A more recent second-client observation beats delayed readback delivery.
    await page.evaluate(() => SC.dial.nudge(1));
    const delayed = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').at(-1));
    await page.evaluate(() => mockSocket.emit({ t: 'state', serverEpoch: 'pc-one', target: { kind: 'session', id: 'app' }, ownerSeq: 16, level: .47, muted: false }));
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'applied', ownerSeq: 15, observation: { level: .33, muted: true } }), delayed);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 47);
    await page.evaluate(() => SC.dial.nudge(1));
    const superseded = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').at(-1));
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'superseded' }), superseded);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 47, 'superseded current control returns to observed state');
    await page.evaluate(() => SC.dial.nudge(1));
    const uncertain = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').at(-1));
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'outcome_unknown' }), uncertain);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 47);
    // Meters own a frame loop only while their page is visibly active.
    await page.click('#tab-devices'); await page.waitForTimeout(120);
    assert(await page.evaluate(() => meterFrames > 0));
    await page.click('#tab-mixer'); const offPage = await page.evaluate(() => meterFrames); await page.waitForTimeout(120);
    assert.strictEqual(await page.evaluate(() => meterFrames), offPage);
    await page.click('#tab-devices'); await page.evaluate(() => startSaver());
    const saverFrames = await page.evaluate(() => meterFrames);
    const hiddenWork = await page.evaluate(value => {
      let count = 0; const observer = new MutationObserver(records => count += records.length);
      observer.observe(document.querySelector('#app'), { childList: true, attributes: true, subtree: true });
      for (let i = 0; i < 100; i++) mockSocket.emit({ ...value, sessions: [{ ...value.sessions[0], ownerSeq: 17 + i, level: (i + 1) / 100 }] });
      count += observer.takeRecords().length; observer.disconnect(); return count;
    }, snapshot());
    assert.strictEqual(hiddenWork, 0, 'saver snapshots must not update the control DOM');
    await page.waitForTimeout(120); assert.strictEqual(await page.evaluate(() => meterFrames), saverFrames);
    await page.click('#saver'); assert.strictEqual(await page.evaluate(() => SC.model.apps[0].level), 100, 'wake ingests the latest retained state');
    await page.click('#tab-mixer'); await page.click('#power-mode');
    assert.strictEqual(await page.evaluate(() => localStorage.getItem('native_power')), 'battery');
    const sentBeforePause = await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').length);
    await page.evaluate(() => { DecksterLifecycle({ foreground: false, powerMode: 'battery' }); SC.dial.nudge(1); });
    await page.waitForTimeout(600);
    assert.strictEqual(await page.evaluate(() => sockets.length), 1, 'background must not reconnect');
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').length), sentBeforePause);
    await page.evaluate(() => DecksterLifecycle({ foreground: true, powerMode: 'battery' }));
    await page.waitForFunction(() => sockets.length === 2);
    assert.strictEqual(await page.evaluate(() => mockSocket.sent.filter(m => m.t === 'set_volume').length), 0, 'wake never replays controls');
    await page.evaluate(value => mockSocket.emit(value), snapshot(1, .24, 'pc-two'));
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 24, 'new server epoch resets observation authority');
    await page.evaluate(command => mockSocket.emit({ ...command, t: 'command_result', serverEpoch: 'pc-one', status: 'applied', ownerSeq: 200, observation: { level: .8, muted: false } }), first);
    assert.strictEqual(await page.evaluate(() => SC.dial.val), 24, 'old-epoch results cannot cross reconnect');
    await page.reload(); await page.waitForFunction(() => window.SC && window.mockSocket);
    assert.strictEqual(await page.textContent('#power-mode'), 'Battery', 'the native mode persists after reload');
    assert.deepStrictEqual(errors, []);
    console.log('phone lifecycle/protocol checks passed; hidden snapshots made zero control DOM mutations');
  } finally { await browser.close(); }
}
run().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
