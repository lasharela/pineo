// End-to-end check of the dashboard in real headless Chrome.
// Usage (hub already running): node dashboard/e2e/verify.mjs [http://127.0.0.1:18931] [screenshot.png]
// Drives a full ticket through /api/debug/* plus fake `app` and `agent` WebSocket clients.
// It calls /api/debug/reset first: point it at a throwaway hub (PINEO_HUB_DATA=/tmp/x), not the demo one.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, '../../fixui/e2e/package.json'));
const { chromium } = require('@playwright/test');

const BASE = process.argv[2] || 'http://127.0.0.1:18931';
const SHOT = process.argv[3] || path.join(here, '../../docs/dashboard.png');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const post = async (p, body) => (await fetch(`${BASE}/api/debug/${p}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
const label = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');

async function socket(role) {
  const msgs = [];
  const ws = new WebSocket(`${BASE.replace('http', 'ws')}/ws?role=${role}`);
  ws.onmessage = (e) => msgs.push(JSON.parse(e.data));
  await new Promise((r) => { ws.onopen = r; });
  return {
    msgs, close: () => ws.close(),
    send: (m) => ws.send(JSON.stringify({ ts: Date.now(), ...m })),
    wait: async (type) => { for (let i = 0; i < 60; i++) { const m = msgs.find((x) => x.type === type); if (m) return m; await sleep(50); } throw new Error(`${role} never got ${type}`); },
  };
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') problems.push(`console.${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  page.on('requestfailed', (r) => problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`http ${r.status()}: ${r.url()}`); });
  const text = (sel) => page.locator(sel).first().evaluate((el) => el.textContent.replace(/\s+/g, ' ').trim());
  const stateIs = (s) => page.waitForFunction((want) => document.getElementById('state-label').textContent === want, s);

  await post('reset', {});
  await page.goto(`${BASE}/dashboard/`);
  await page.waitForSelector('#pill-hub', { state: 'hidden' });
  await stateIs('No session');
  assert.match(await text('#bug-list'), /No tickets yet\. Rotate twice to start a session\./);
  assert.equal((await post('gesture', { name: 'single' })).accepted, false); // a single rotation outside a session is ignored
  assert.equal((await post('gesture', { name: 'double' })).action, 'session.start');
  await stateIs('Session live');
  assert.match(await text('#bug-list'), /No tickets yet\. Rotate once to report one\./);
  await page.waitForFunction(() => getComputedStyle(document.querySelector('#live .state-line')).backgroundColor === 'rgb(47, 91, 234)'); // session is blue (after the colour transition)
  assert.match(await text('#state-timer'), /^\d:\d\d$/);
  assert.match(await text('#bug-list .group.now'), /Session live · since \d\d:\d\d · 0 sent/);

  // fake phone app and coding agent on the socket
  const app = await socket('app');
  const agent = await socket('agent');
  app.send({ type: 'app.context', screen: 'cards', visibleElementIds: ['card-block-toggle'], lastTappedElementId: 'card-block-toggle', viewport: { width: 390, height: 844 } });
  await post('band', { connected: true, listening: true, battery: 82, transport: 'ble' });
  await page.waitForFunction(() => document.getElementById('phone-text').textContent === 'Phone on Cards');
  assert.equal(await text('#band-text'), 'mic on');
  assert.equal(await text('#band-battery-text'), '82 %');
  assert.match(await text('#gesture-chip'), /^Double rotation → session started, \d\d:\d\d:\d\d$/);

  // an earlier, finished ticket so the list has history
  await post('gesture', { name: 'open' });
  await post('transcript', { text: 'The monthly limit text is too small to read.', final: true });
  const early = await post('gesture', { name: 'submit' });
  await post('enrich', { bugId: early.bug.id, kind: 'improvement', title: 'Make the monthly limit easier to read', summary: 'The limit amount is small and low contrast.', severity: 'low', elementIds: ['card-limit'] });
  await post('fix', { bugId: early.bug.id, state: 'done', summary: 'Raised the limit amount to 20px, bold.', files: ['src/screens/Cards.tsx'], ms: 9200 });
  app.msgs.length = 0; agent.msgs.length = 0;

  // the main ticket: open by gesture
  const opened = await post('gesture', { name: 'open' });
  assert.equal(opened.accepted, true);
  const bugId = (await app.wait('bug.opened')).bugId;
  assert.equal(bugId, opened.bug.id);
  await stateIs('Reporting…');
  assert.equal(await page.locator('#live.recording').count(), 1);
  assert.equal(await page.locator('.bug').count(), 2);
  assert.match(await text('.bug.selected'), /Reporting/);
  assert.equal(await text('#bug-count'), '1 sent'); // counts agree: the open ticket is not sent yet
  assert.match(await text('#bug-list .group.now'), /1 sent$/);
  assert.match(await text('#replay-note'), /Capturing taps and circles/);
  assert.equal((await post('gesture', { name: 'open' })).accepted, false); // a second open is ignored
  assert.equal(await page.locator('.bug').count(), 2);

  // live transcript: interim lighter, finals solid
  await post('transcript', { text: 'When I tap block card', final: false });
  await page.waitForFunction(() => document.querySelector('#live-text .interim')?.textContent === 'When I tap block card');
  await post('transcript', { text: 'When I tap block card the switch flips back by itself.', final: true });
  app.send({ type: 'app.interaction', bugId, items: [
    { at: Date.now(), kind: 'screen', screen: 'cards' },
    { at: Date.now(), kind: 'tap', selected: true, screen: 'cards', elementId: 'card-freeze-help', label: 'Freeze help', rect: { x: 16, y: 250, w: 358, h: 50 } },
    { at: Date.now(), kind: 'tap', selected: true, screen: 'cards', elementId: 'card-block-toggle', label: 'Block card', rect: { x: 16, y: 330, w: 358, h: 62 } },
    { at: Date.now(), kind: 'tap', selected: false, screen: 'cards', elementId: 'card-freeze-help', label: 'Freeze help', rect: { x: 16, y: 250, w: 358, h: 50 } }] });
  await post('transcript', { text: 'the card', final: false });
  await page.waitForFunction(() => document.querySelector('#live-text .final')?.textContent.includes('flips back by itself')
    && document.querySelector('#live-text .interim')?.textContent === 'the card');
  await post('transcript', { text: 'The card is still active, and this limit row looks wrong too.', final: true });
  const loop = Array.from({ length: 25 }, (_, k) => { const a = (k / 24) * Math.PI * 2 - 1.1; return [+(0.5 + 0.45 * Math.cos(a)).toFixed(3), +(0.555 + 0.06 * Math.sin(a)).toFixed(3)]; });
  app.send({ type: 'app.interaction', bugId, items: [{ at: Date.now(), kind: 'circle', screen: 'cards', elementIds: ['card-limit'], path: loop }] });
  await page.waitForFunction(() => document.querySelectorAll('#detail-text .lines li').length === 7); // 2 spoken + screen + 3 taps + circle
  assert.match(await text('.bug.selected .bug-title'), /^When I tap block card/); // first words until enriched
  assert.match(await text('#state-timer'), /^\d:\d\d$/);
  assert.equal(await page.locator('#phone-screen .pt').count(), 2); // the selected tap and the drawn circle; the deselected one is not drawn
  assert.equal(await page.locator('#phone-screen .pt-loop').count(), 1);
  assert.match(await text('#detail-text .lines'), /Selected Freeze help.*Selected Block card.*Deselected Freeze help/);
  assert.doesNotMatch(await text('#detail-text .chips'), /freeze/i); // not meant after all
  assert.match(await text('#detail-text .lines'), /Circled/);

  // a report with nothing said or pointed at is closed, not filed (checked on the earlier, still-quiet state)
  // submit: the app and the agent are told, the card animates in as submitted
  await post('gesture', { name: 'submit' });
  await app.wait('bug.submitted');
  const handed = await agent.wait('bug.submitted');
  assert.equal(handed.bug.interactions.length, 5);
  assert.deepEqual(handed.bug.elementIds, ['card-block-toggle', 'card-limit']);
  await stateIs('Ticket sent');
  assert.match(await text('.bug.selected'), /Sent/);
  assert.equal(await page.locator('.bug.selected.enter').count(), 1);
  assert.match(await text('#detail-text .summary'), /Writing up the ticket/);
  assert.match(await text('#detail-text .lines'), /Closed by wrist rotation$/);
  // the rest of the sentence arrives after the ticket closed: appended, marked, and re-sent to the agent
  agent.msgs.length = 0;
  await post('transcript', { text: 'It should stay on.', final: true });
  await page.waitForFunction(() => /It should stay on\. added after closing$/.test(document.querySelector('#detail-text .lines')?.textContent.replace(/\s+/g, ' ').trim()));
  assert.equal((await agent.wait('bug.submitted')).amended, true);
  agent.msgs.length = 0;

  // write-up arrives
  await post('enrich', { bugId, kind: 'bug', title: 'Block card switch flips back by itself', summary: 'Tapping Block card turns the switch on for a moment, then it returns to off and the card stays active.', severity: 'high', elementIds: ['card-block-toggle', 'card-limit'], steps: ['Open Cards', 'Tap the Block card switch', 'Watch it return to off'] });
  await page.waitForFunction(() => document.querySelector('#detail-text h1')?.textContent === 'Block card switch flips back by itself');
  assert.match(await text('#detail-text'), /stays active/);
  assert.equal(await page.getAttribute('#detail-text .chips .chip', 'title'), 'card-block-toggle');
  assert.equal(await page.locator('#detail-text .steps li').count(), 3);
  assert.match(await text('.bug.selected .bug-top'), /bug.*high/i);
  assert.equal((await agent.wait('bug.upsert')).bug.kind, 'bug');

  // the coding agent reports its fix
  agent.send({ type: 'bug.fix', bugId, state: 'working' });
  await stateIs('Agent fixing…'); // the big pill follows the agent
  assert.match(await text('#state-timer'), /^0:0\d$/);
  assert.equal(await page.locator('#live.fixing').count(), 1);
  await page.waitForFunction(() => /Agent fixing… 0:0\d/.test(document.querySelector('#detail-text .fix')?.textContent));
  assert.match(await text('.bug.selected .bug-top'), /Agent fixing/);
  await app.wait('bug.fix');
  assert.ok(app.msgs.some((m) => m.type === 'bug.fix' && m.bugId === bugId && m.state === 'working')); // the phone is told too
  agent.send({ type: 'bug.fix', bugId, state: 'done', summary: 'The switch now keeps its state and blocks the card.', files: ['src/screens/Cards.tsx', 'src/state/cards.ts'], ms: 14200 });
  await page.waitForFunction(() => /Done in 14 s/.test(document.querySelector('#detail-text .fix')?.textContent));
  assert.match(await text('#detail-text .fix'), /keeps its state.*src\/screens\/Cards\.tsx/);
  assert.match(await text('.bug.selected .bug-top'), /Fixed live/);
  await stateIs('Fixed live · 14 s');
  assert.equal(await text('#state-sub'), 'The switch now keeps its state and blocks the card.');
  assert.equal(await page.locator('#live.done').count(), 1);
  assert.match(await text('#detail-text .said'), /^“When I tap block card the switch flips back by itself\. The card is still active.*It should stay on\. added after closing”$/);
  const order = await page.locator('#detail-text h3').allTextContents(); // spoken words sit right under the fix
  assert.deepEqual(order, ['What was said', 'Summary', 'Elements involved', 'Steps', 'Timeline']);

  // assist feed
  await post('transcript', { text: 'Where do I freeze my card?', final: true });
  await post('assist', { elementId: 'card-block-toggle', say: 'Open Cards and use the Block card switch.' });
  await app.wait('assist');
  await page.waitForSelector('.assist');
  assert.match(await text('.assist'), /Where do I freeze my card\?.*Sent to the phone/);
  app.send({ type: 'app.assist.shown', elementId: 'card-block-toggle', ok: true });
  await page.waitForFunction(() => /Highlighted on the phone/.test(document.querySelector('.assist')?.textContent));
  await post('transcript', { text: 'How do I turn on dark mode?', final: true });
  await post('assist', { say: 'I could not find a dark mode setting.' });
  await page.waitForFunction(() => document.querySelectorAll('.assist').length === 2);
  assert.match(await text('.assist'), /dark mode\?.*No matching element/);

  // an empty report is closed without a ticket and never reaches the agent
  agent.msgs.length = 0;
  await post('gesture', { name: 'open' });
  await stateIs('Reporting…');
  assert.equal(await page.locator('.bug').count(), 3);
  const empty = await post('gesture', { name: 'submit' });
  assert.equal(empty.bug.status, 'cancelled');
  await stateIs('Nothing captured');
  await page.waitForFunction(() => document.querySelectorAll('.bug').length === 2);
  assert.match(await text('#bug-list .dropped'), /^1 closed with nothing captured/);
  assert.match(await text('.bug.selected .bug-title'), /Block card switch/); // selection falls back to a real ticket
  assert.ok(!agent.msgs.some((m) => m.type === 'bug.submitted'));

  // screenshot of the settled state
  await stateIs('Session live');
  assert.match(await text('#state-sub'), /^2 sent · mic on · rotate once/);
  const box = await page.locator('#phone-screen').boundingBox(); // phone frame: real proportions, uses the panel height
  assert.ok(Math.abs(box.height / box.width - 844 / 390) < 0.02, `aspect ${box.height / box.width}`);
  assert.ok(box.height >= 520, `phone screen only ${box.height}px tall`);
  const panel = await page.locator('.detail-panel').boundingBox();
  const ctl = await page.locator('.controls').boundingBox();
  assert.ok(ctl.y + ctl.height <= panel.y + panel.height, 'phone and its controls fit inside the panel');
  const tapBox = await page.locator('#phone-screen .pt-rect').boundingBox(); // the tap is still drawn where it happened
  assert.ok(Math.abs((tapBox.y - box.y) / box.height - 330 / 844) < 0.02 && Math.abs(tapBox.width / box.width - 358 / 390) < 0.03);
  assert.match(await text('#bug-list .group.now'), /2 sent/);
  await page.mouse.move(700, 890);
  await sleep(2600); // entry animations finish
  await page.screenshot({ path: SHOT });

  // debug panel drives everything with no band and no phone
  assert.equal(await page.locator('#debug').isVisible(), false);
  await page.keyboard.press('d');
  assert.equal(await page.locator('#debug').isVisible(), true);
  await page.click('#debug [data-gesture=single]');
  await stateIs('Reporting…');
  await page.fill('#dbg-text', 'typed from the debug panel');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => /typed from the debug panel/.test(document.getElementById('live-text').textContent));
  assert.equal(await page.locator('#debug').isVisible(), true); // typing "d" in the input did not close it
  await page.click('#dbg-tap');
  await page.click('#dbg-circle');
  await page.waitForFunction(() => document.querySelectorAll('#phone-screen .pt').length === 2);
  await page.click('#debug [data-gesture=submit]');
  await page.waitForFunction(() => document.querySelectorAll('.bug').length === 3 && /Sent/.test(document.querySelector('.bug').textContent));
  await page.click('#dbg-enrich');
  await page.waitForFunction(() => document.querySelector('#detail-text h1')?.textContent === 'Typed from the debug panel');
  await page.click('#dbg-fix');
  await page.waitForFunction(() => /Fixed live/.test(document.querySelector('.bug')?.textContent), null, { timeout: 8000 });
  await post('fix', { bugId, state: 'working' }); // a failed fix is a calm state on the big pill
  await post('fix', { bugId, state: 'failed', summary: 'The switch is driven by test data.' });
  await stateIs('Couldn’t change that');
  assert.equal(await page.locator('#live.failed').count(), 1);
  await post('fix', { bugId, state: 'done', summary: 'The switch now keeps its state and blocks the card.', ms: 14200 });
  await page.fill('#dbg-element', 'card-block-toggle');
  await page.click('#dbg-assist');
  await page.waitForFunction(() => document.querySelectorAll('.assist').length === 3);
  await page.click('#debug [data-gesture=double]'); // end the session from the panel
  await stateIs('Session ended · 3 sent');
  assert.match(await text('#gesture-chip'), /^Double rotation → session ended, \d\d:\d\d:\d\d$/);
  assert.equal(await text('#band-text'), 'mic on'); // hardware status is unchanged by the session ending
  await post('band', { connected: true, listening: true, battery: 8, transport: 'ble' });
  await page.waitForFunction(() => document.getElementById('band-battery-text').textContent === '8 % · charge the band');
  assert.equal(await page.locator('#pill-band.lowbat').count(), 1); // the whole chip turns red
  await post('band', { connected: true, listening: true, battery: 82, transport: 'ble', stt: 'reconnecting' });
  await page.waitForFunction(() => document.getElementById('band-text').textContent === 'speech reconnecting…');
  assert.equal(await page.locator('#pill-band.lowbat').count(), 0);
  await post('band', { connected: true, listening: true, battery: 82, transport: 'ble' });
  await page.waitForFunction(() => /^Session ended \d\d:\d\d · 3 sent$/.test(document.querySelector('#bug-list .group')?.textContent.trim()));
  assert.equal(await page.locator('#bug-list .group.now').count(), 0);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#debug').isVisible(), false);
  await stateIs('No session');

  // a reload gets the whole state back from hello
  await page.reload();
  await page.waitForFunction(() => document.querySelectorAll('.bug').length === 3);
  assert.match(await text('#bug-list .group'), /^Session ended \d\d:\d\d · 3 sent$/);
  assert.equal(await text('#state-label'), 'No session');
  await page.click(`.bug[data-id="${bugId}"]`);
  await page.waitForFunction(() => document.querySelectorAll('#phone-screen .pt').length === 2);
  assert.match(await text('#detail-text .fix'), /Done in 14 s/);
  assert.equal(await page.locator('.assist').count(), 3);
  assert.equal(await text('#band-text'), 'mic on');

  // band connect / disconnect button, with a fake band process on the socket
  assert.equal(await text('#band-toggle'), 'Disconnect'); // debug band state says connected, but no band process:
  await page.click('#band-toggle');
  await page.waitForFunction(() => document.getElementById('band-hint').textContent === 'Band process not running');
  assert.ok(problems.length <= 2 && problems.every((p) => /409/.test(p)), `unexpected: ${problems}`); // that refusal is the only error so far
  problems.length = 0;
  const band = await socket('band');
  band.send({ type: 'band.state', connected: true, listening: false, battery: 82, transport: 'ble', link: 'connected' });
  await post('gesture', { name: 'double' });
  await stateIs('Session live');
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'Disconnect' && !document.getElementById('band-toggle').disabled);
  await page.click('#band-toggle'); // a session is live: asks once more first
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'End session and disconnect?');
  assert.ok(!band.msgs.some((m) => m.type === 'band.control'));
  await page.click('#band-toggle');
  assert.equal((await band.wait('band.control')).action, 'disconnect');
  assert.ok(band.msgs.findIndex((m) => m.type === 'session.ended') < band.msgs.findIndex((m) => m.type === 'band.control')); // session ended first
  band.send({ type: 'band.state', connected: false, listening: false, transport: 'ble', link: 'disconnected' });
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'Connect' && !document.getElementById('band-toggle').disabled);
  assert.equal(await text('#band-text'), 'mic off');
  assert.equal(await page.getAttribute('#band-toggle', 'data-link'), 'disconnected'); // grey dot
  band.msgs.length = 0;
  await page.click('#band-toggle');
  assert.equal((await band.wait('band.control')).action, 'connect');
  band.send({ type: 'band.state', connected: false, listening: false, transport: 'ble', link: 'searching', hint: 'Double-tap the band so it blinks blue' });
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'Connecting…' && document.getElementById('band-toggle').disabled);
  assert.equal(await text('#band-hint'), 'Double-tap the band so it blinks blue');
  assert.equal(await page.getAttribute('#band-toggle', 'data-link'), 'connecting'); // yellow dot
  band.send({ type: 'band.state', connected: true, listening: false, battery: 80, transport: 'ble', link: 'connected' });
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'Disconnect');
  assert.equal(await page.getAttribute('#band-toggle', 'data-link'), 'connected'); // green dot
  assert.equal(await page.locator('#band-toggle .dot').evaluate((el) => getComputedStyle(el).backgroundColor), 'rgb(52, 208, 140)');
  assert.equal(await page.locator('#pill-band .dot').count(), 0); // the chip has no dot and no "Band connected" text
  assert.equal(await page.locator('#band-hint').isVisible(), false);
  band.close();
  await page.waitForFunction(() => document.getElementById('band-toggle').textContent === 'Band process not running' && document.getElementById('band-toggle').disabled && document.getElementById('pill-band').hidden);

  app.close(); agent.close();
  assert.deepEqual(problems, [], 'no console errors, page errors or failed requests');
  console.log(`PASS: dashboard verified in headless Chrome ${browser.version()}; screenshot ${path.relative(process.cwd(), SHOT)}`);
} finally {
  await browser.close();
}
