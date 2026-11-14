// End-to-end check in real headless Chrome with a phone viewport and touch.
// Serves ./dist plus a stand-in hub (WebSocket /ws + /api/debug/*) on one origin, like the real hub does.
// Usage: pnpm build && pnpm e2e     (screenshots go to ../docs/mobile-*.png)
import http from "node:http";
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import { join, dirname, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { WebSocketServer } from "ws";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const docs = join(root, "..", "docs");
const require = createRequire(join(root, "..", "fixui", "e2e", "package.json"));
const { chromium } = require("@playwright/test");
mkdirSync(docs, { recursive: true });

const PORT = 8797;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 6000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(60);
  }
}

// ---- stand-in hub -------------------------------------------------------
const received = []; // every message from the app
const clients = new Set();
const send = (type, fields = {}) => {
  const text = JSON.stringify({ type, ts: Date.now(), ...fields });
  for (const c of clients) c.send(text);
};
let openBugId = null;
let helloBugs = []; // what the stand-in hub reports in hello.state.bugs
let bugSeq = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (req.method === "POST" && url.pathname.startsWith("/api/debug/")) {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const data = JSON.parse(body || "{}");
      const route = url.pathname.split("/").pop();
      if (route === "gesture" && data.name === "open") send("bug.opened", { bugId: (openBugId = `bug_e2e_${++bugSeq}`) });
      else if (route === "gesture" && data.name === "submit") send("bug.submitted", { bugId: openBugId });
      else if (route === "gesture") send("bug.cancelled", { bugId: openBugId });
      else if (route === "transcript") send("transcript", data);
      else if (route === "assist") send("assist", data);
      res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
    });
    return;
  }
  let file = join(dist, url.pathname === "/" ? "index.html" : url.pathname);
  if (!existsSync(file)) file = join(dist, "index.html");
  res.writeHead(200, { "content-type": MIME[extname(file)] ?? "application/octet-stream" }).end(readFileSync(file));
});
const wss = new WebSocketServer({ noServer: true });
server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url, "http://x");
  if (url.pathname !== "/ws" || url.searchParams.get("role") !== "app") return socket.destroy();
  wss.handleUpgrade(req, socket, head, (ws) => {
    clients.add(ws);
    ws.on("close", () => clients.delete(ws));
    ws.on("message", (d) => received.push(JSON.parse(String(d))));
    ws.send(JSON.stringify({ type: "hello", ts: Date.now(), state: { bugs: helloBugs, band: { connected: true, listening: false } } }));
  });
});
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));
const base = `http://127.0.0.1:${PORT}`;

// ---- browser ------------------------------------------------------------
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
  userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
});
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));
const shot = (name) => page.screenshot({ path: join(docs, `mobile-${name}.png`) });
const lastCtx = () => received.filter((m) => m.type === "app.context").at(-1);

try {
  // 1. every screen renders
  await page.goto(base + "/");
  await until(() => clients.size === 1);
  check("websocket connects as role=app", clients.size === 1);
  for (const screen of ["home", "cards", "transfer", "activity", "profile"]) {
    await page.locator(`[data-pineo-id="tab-${screen}"]`).tap();
    await page.waitForSelector(`.app[data-screen="${screen}"]`);
    await sleep(450);
    await shot(screen);
    const ids = await page.locator(".scroll [data-pineo-id]").count();
    const ctx = await until(() => (lastCtx()?.screen === screen ? lastCtx() : null), 3000);
    check(`screen ${screen} renders (${ids} marked elements) and reports app.context`, ids > 5 && !!ctx,
      ctx ? `${ctx.visibleElementIds.length} visible, lastTapped=${ctx.lastTappedElementId}` : "no context");
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  check("no horizontal page overflow at 390px", !overflow);

  // 1a. no session: band listening must not animate anything
  send("band.state", { connected: true, listening: true, transport: "ble" });
  await sleep(400);
  const idle = await page.evaluate(() => ({ dot: !!document.querySelector(".pineo-band-dot"), animated: [...document.querySelectorAll(".pineo-root *")].filter((e) => getComputedStyle(e).animationName !== "none").map((e) => e.className) }));
  check("no session: nothing on screen at all, nothing animated", !idle.dot && idle.animated.length === 0, JSON.stringify(idle));
  check("app.diag hello (ua, view, build) is sent on connect", received.some((m) => m.type === "app.diag" && m.items[0].ev === "hello" && m.items[0].ua && m.items[0].build && m.items[0].view.length === 3));

  // 1b. session: calm indicator, app fully usable
  send("session.started", { sessionId: "sess_e2e", startedAt: Date.now() });
  await page.waitForSelector(".pineo-pill-session");
  await page.locator('[data-pineo-id="tab-cards"]').tap();
  await page.locator('[data-pineo-id="card-block-toggle"] .toggle').tap();
  const sessUsable = await page.locator('[data-pineo-id="card-block-toggle"] .toggle').getAttribute("aria-checked");
  await page.locator('[data-pineo-id="card-block-toggle"] .toggle').tap();
  await shot("session");
  check("session shows a chip + hairline and the app stays usable (no selector mode)",
    /^Session0:0\d$/.test((await page.locator(".pineo-pill-session").textContent()) ?? "") && (await page.locator(".pineo-session-line").count()) === 1 && sessUsable === "true" && (await page.locator(".pineo-frame").count()) === 0);

  // 2. ticket: open → tap, type, scroll, circle → submit
  const cdp = await context.newCDPSession(page);
  const touch = (type, x, y) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: type === "touchEnd" ? [] : [{ x, y }] });
  const items = (bugId) => received.filter((m) => m.type === "app.interaction" && m.bugId === bugId).flatMap((m) => m.items);
  await page.locator('[data-pineo-id="tab-home"]').tap();
  send("band.state", { connected: true, listening: true, transport: "ble" });
  send("bug.opened", { bugId: "bug_e2e_a" });
  await page.waitForSelector(".pineo-pill-rec");
  const hint = await page.locator(".pineo-hint").textContent();
  send("transcript", { text: "this total", final: false });
  await sleep(300);
  send("transcript", { text: "This total does not add up.", final: true });
  send("transcript", { text: "look at these two accounts", final: false });
  await sleep(500);
  const capt = await page.evaluate(() => ({
    final: document.querySelector(".pineo-final")?.textContent,
    interim: document.querySelector(".pineo-interim")?.textContent,
    pill: document.querySelector(".pineo-pill-rec")?.textContent,
  }));
  check("neutral ticket pill, hint, timer and captions show", /Listening… describe it/.test(capt.pill ?? "") && !/bug/i.test(capt.pill ?? "") && (hint ?? "") === "Tap or circle what you mean" && /add up/.test(capt.final ?? "") && /two accounts/.test(capt.interim ?? ""), JSON.stringify(capt));
  await shot("reporting");

  { // the dock sits above selection marks, and there is room to scroll anything above it
    const pad = await page.evaluate(() => ({ dockZ: getComputedStyle(document.querySelector(".pineo-dock")).zIndex, space: getComputedStyle(document.querySelector(".screen")).paddingBottom }));
    check("ticket open: dock is layered above marks and the screen gains scroll room under it", pad.dockZ === "2" && pad.space === "292px", JSON.stringify(pad)); }

  // 2a. hold, then draw a loop around two account rows
  const loopAround = async (selA, selB, name) => {
    const box = await page.evaluate(([a, b]) => {
      const ra = document.querySelector(a).getBoundingClientRect();
      const rb = document.querySelector(b).getBoundingClientRect();
      return { l: Math.min(ra.left, rb.left), t: Math.min(ra.top, rb.top), r: Math.max(ra.right, rb.right), b: Math.max(ra.bottom, rb.bottom) };
    }, [selA, selB]);
    const cx = (box.l + box.r) / 2, cy = (box.t + box.b) / 2;
    const rx = Math.min((box.r - box.l) / 2 + 6, cx - 3), ry = (box.b - box.t) / 2 + 4;
    await touch("touchStart", cx + rx, cy);
    await sleep(450); // a long hold (past the long-press point), then the drag
    for (let i = 1; i <= 34; i++) {
      const a = (i / 32) * Math.PI * 2;
      await touch("touchMove", cx + rx * Math.cos(a), cy + ry * Math.sin(a));
      await sleep(12);
      if (i === 20 && name) await shot(name);
    }
    await touch("touchEnd");
  };
  const scrollBefore = await page.evaluate(() => document.querySelector(".scroll").scrollTop);
  await loopAround('[data-pineo-id="home-account-savings"]', '[data-pineo-id="home-account-travel"]', "circle-drawing");
  await sleep(250);
  const pickedNow = await page.evaluate(() => [...document.querySelectorAll(".pineo-mark-on")].map((e) => e.getAttribute("data-pineo-mark")));
  await shot("circle-picked");
  const circle = await until(() => items("bug_e2e_a").find((i) => i.kind === "circle"), 3000);
  const scrollAfter = await page.evaluate(() => document.querySelector(".scroll").scrollTop);
  check("held loop around two accounts sends a circle with exactly those elements",
    !!circle && JSON.stringify(circle.elementIds) === JSON.stringify(["home-account-savings", "home-account-travel"]) && circle.path.length >= 12 && circle.path.every(([x, y]) => x >= 0 && x <= 1 && y >= 0 && y <= 1),
    circle ? JSON.stringify({ elementIds: circle.elementIds, label: circle.label, pathPoints: circle.path.length, rect: circle.rect }) : "no circle");
  check("circled elements stay marked as selected and the page did not scroll while drawing", pickedNow.length === 2 && scrollBefore === scrollAfter, `picked=${pickedNow} scroll ${scrollBefore}→${scrollAfter}`);

  // 2b. a normal drag still scrolls and is not a circle
  await touch("touchStart", 195, 600);
  for (let y = 580; y >= 300; y -= 20) { await touch("touchMove", 195, y); await sleep(12); }
  await touch("touchEnd");
  await sleep(500);
  const scrolled = await page.evaluate(() => document.querySelector(".scroll").scrollTop);
  check("normal drag still scrolls while a ticket is open and is not read as a circle", scrolled > scrollAfter + 100 && items("bug_e2e_a").filter((i) => i.kind === "circle").length === 1, `scrollTop=${Math.round(scrolled)}`);

  // 2c. selector mode: taps select, they do not operate the app
  const marks = () => page.evaluate(() => [...document.querySelectorAll(".pineo-mark-on")].map((e) => e.getAttribute("data-pineo-mark")));
  const tapOn = async (sel) => { const b = await page.locator(sel).boundingBox(); await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2); await sleep(250); };
  await page.locator('[data-pineo-id="home-goal-add"]').scrollIntoViewIfNeeded();
  await sleep(300);
  await tapOn('[data-pineo-id="home-goal-add"]');
  const addMoney = { toast: await page.locator(".toast").count(), marks: await marks(), chip: await page.locator('.pineo-mark[data-pineo-mark="home-goal-add"] span').textContent().catch(() => null) };
  await tapOn('[data-pineo-id="home-savings-goal"] .goal-text small'); // a paragraph
  const para = await marks();
  check("tap on the Add money button selects it (label chip) and adds no money; tap on a paragraph selects its card",
    addMoney.toast === 0 && addMoney.marks.includes("home-goal-add") && addMoney.chip === "Add money to goal" && para.includes("home-savings-goal"), JSON.stringify({ addMoney, para }));
  await tapOn('[data-pineo-id="home-goal-add"]');
  check("tapping a selected element again deselects it", !(await marks()).includes("home-goal-add"));
  // long press (no drag) selects while the finger is still down
  { const b = await page.locator('[data-pineo-id="home-spending-summary"] .spend-head').boundingBox();
    await touch("touchStart", b.x + 40, b.y + 10); await sleep(480);
    const during = await marks(); const pulse = await page.locator(".pineo-picked").count();
    await touch("touchEnd"); await sleep(250);
    const afterUp = await marks();
    check("long press selects while the finger is down, with a pulse, and releasing does not undo it", during.includes("home-spending-summary") && pulse === 1 && afterUp.includes("home-spending-summary"), JSON.stringify({ during, pulse })); }
  await page.locator('[data-pineo-id="tab-cards"]').tap(); // tabs still switch screens
  await page.waitForSelector('.app[data-screen="cards"]');
  await tapOn('[data-pineo-id="card-block-toggle"] .toggle');
  const blocked = await page.locator('[data-pineo-id="card-block-toggle"] .toggle').getAttribute("aria-checked");
  await page.locator('[data-pineo-id="tab-transfer"]').tap();
  await page.waitForSelector('.app[data-screen="transfer"]');
  await page.locator('[data-pineo-id="transfer-amount"]').scrollIntoViewIfNeeded();
  await tapOn('[data-pineo-id="transfer-amount"] input');
  const focus = await page.evaluate(() => document.activeElement?.tagName);
  await tapOn('[data-pineo-id="transfer-from-account"] select');
  const selState = await page.evaluate(() => ({ focus: document.activeElement?.tagName, selection: String(getSelection()).length }));
  const m2 = await marks();
  check("tap on a toggle, a text field and a dropdown changes nothing (no toggle, no focus/keyboard) and selects them",
    blocked === "false" && focus !== "INPUT" && selState.focus !== "SELECT" && selState.selection === 0 && m2.includes("transfer-amount") && m2.includes("transfer-from-account"),
    JSON.stringify({ blocked, focus, selState, marks: m2 }));
  await shot("selector");
  // assist while a ticket is open: navigates, scrolls and pulses; selector mode stays on; nothing gets selected
  { const before = await marks(); const n = received.filter((m) => m.type === "app.assist.shown").length;
    send("assist", { elementId: "activity-export-statement", say: "Export is at the bottom of Activity." });
    const reply = await until(() => received.filter((m) => m.type === "app.assist.shown")[n], 7000);
    await sleep(300);
    const st = await page.evaluate(() => ({ screen: document.querySelector(".app").getAttribute("data-screen"), halo: !!document.querySelector(".pineo-halo"), mode: document.documentElement.classList.contains("pineo-ticket"), sel: window.__pineo.getState().selected.length }));
    check("assist works during a ticket: navigates and pulses, selector mode stays on, no selection added", reply?.ok === true && st.screen === "activity" && st.halo && st.mode && st.sel === before.length + 0 && st.sel >= 1, JSON.stringify(st));
    await page.locator('[data-pineo-id="tab-transfer"]').tap(); await page.waitForSelector('.app[data-screen="transfer"]'); }
  await sleep(800);
  const beforeClosing = received.filter((m) => m.type === "app.interaction").flatMap((m) => m.items).length;
  await tapOn('[data-pineo-id="transfer-currency"] select');
  send("bug.closing", { bugId: "bug_e2e_a" });
  await page.waitForSelector(".pineo-pill-closing");
  await sleep(150);
  const closing = { text: await page.locator(".pineo-pill-closing").textContent(), mode: await page.evaluate(() => document.documentElement.classList.contains("pineo-ticket")), marks: (await marks()).length, flushed: received.filter((m) => m.type === "app.interaction").flatMap((m) => m.items).length > beforeClosing };
  check('bug.closing leaves selector mode at once, flushes pending interactions and shows "Closing…"', closing.text === "Closing…" && !closing.mode && closing.marks === 0 && closing.flushed, JSON.stringify(closing));
  await sleep(500);
  send("bug.submitted", { bugId: "bug_e2e_a" });
  await page.waitForSelector(".pineo-pill-submitted");
  const sentText = await page.locator(".pineo-pill-submitted").textContent();
  await shot("submitted");
  await sleep(400);
  const all = items("bug_e2e_a");
  const taps = all.filter((i) => i.kind === "tap").map((i) => `${i.elementId}${i.selected === false ? ":off" : ""}`);
  const tapBlock = all.find((i) => i.kind === "tap" && i.elementId === "card-block-toggle");
  check("selections are reported as tap interactions with element id and label; tab taps are not",
    tapBlock?.label === "Block card" && tapBlock.screen === "cards" && taps.join(",") === "home-accounts,home-accounts:off,home-goal-add,home-savings-goal,home-goal-add:off,home-spending-summary,card-block-toggle,transfer-amount,transfer-from-account,transfer-currency",
    `taps=${taps.join(",")}`);
  check("screen changes are reported", all.filter((i) => i.kind === "screen").map((i) => i.screen).join(",") === "home,cards,transfer,activity,transfer", `kinds=${all.map((i) => i.kind).join(",")}`);
  check('submit shows "Ticket sent" and clears the selection marks', sentText === "Ticket sent" && (await marks()).length === 0 && !(await page.evaluate(() => document.documentElement.classList.contains("pineo-ticket"))), sentText);
  // the app is an app again
  await page.locator('[data-pineo-id="transfer-amount"] input').tap();
  const focusAfter = await page.evaluate(() => document.activeElement?.tagName);
  await page.locator('[data-pineo-id="transfer-amount"] input').fill("12abc");
  await page.locator('[data-pineo-id="tab-home"]').tap();
  await page.locator('[data-pineo-id="home-goal-add"]').tap();
  const toastAfter = await page.locator(".toast").textContent().catch(() => null);
  await page.locator('[data-pineo-id="tab-cards"]').tap();
  await page.locator('[data-pineo-id="card-block-toggle"] .toggle').tap();
  const blockedAfter = await page.locator('[data-pineo-id="card-block-toggle"] .toggle').getAttribute("aria-checked");
  await page.locator('[data-pineo-id="card-block-toggle"] .toggle').tap();
  check("after submit the same taps work again (field focuses, Add money adds, toggle toggles)", focusAfter === "INPUT" && /Added \$25/.test(toastAfter ?? "") && blockedAfter === "true", JSON.stringify({ focusAfter, toastAfter, blockedAfter }));
  const n1 = received.filter((m) => m.type === "app.interaction").length;
  await page.locator('[data-pineo-id="tab-home"]').tap();
  await sleep(1000);
  check("nothing is reported once the ticket is closed", received.filter((m) => m.type === "app.interaction").length === n1);

  send("session.ended", { sessionId: "sess_e2e", endedAt: Date.now(), tickets: 1 });
  await page.waitForSelector(".pineo-session-ended");
  check('session end shows "Session ended · 1 ticket" and removes the indicator', (await page.locator(".pineo-session-ended").textContent()) === "Session ended · 1 ticket" && (await page.locator(".pineo-session-line").count()) === 0);

  await sleep(3800);
  const afterSession = await page.evaluate(() => [...document.querySelectorAll(".pineo-root *")].filter((e) => getComputedStyle(e).animationName !== "none").map((e) => e.className));
  check("after the session ends nothing is animated", afterSession.length === 0, JSON.stringify(afterSession));
  const diagEvs = received.filter((m) => m.type === "app.diag").flatMap((m) => m.items.map((i) => i.ev));
  check("app.diag carried touchstart, touchend, tap and select items during the ticket", ["touchstart", "touchend", "tap", "select", "circle"].every((e) => diagEvs.includes(e)), [...new Set(diagEvs)].join(","));

  // 3. coding agent progress
  send("bug.fix", { bugId: "bug_e2e_a", state: "working" });
  await page.waitForSelector(".pineo-fix-working");
  const working = await page.locator(".pineo-pill-fix").textContent();
  await page.locator('[data-pineo-id="tab-cards"]').tap();
  send("bug.fix", { bugId: "bug_e2e_a", state: "done", summary: "Block card status line now matches the switch", files: ["src/screens/Cards.tsx"], ms: 9000 });
  await page.waitForSelector(".pineo-fix-done");
  const done = await page.locator(".pineo-pill-fix").textContent();
  const flashed = await until(async () => (await page.locator('.pineo-picked[data-pineo-picked="card-block-toggle"]').count()) === 1, 3000);
  await shot("fix-done");
  check('bug.fix shows "Working on it…" then "Done: …" and flashes the touched element', working === "Working on it…" && /^Done: Block card status/.test(done ?? "") && !!flashed, `${working} / ${done}`);
  send("bug.fix", { bugId: "bug_e2e_a", state: "failed", summary: "build error" });
  await page.waitForSelector(".pineo-fix-failed");
  check("failed fix shows a calm line", (await page.locator(".pineo-fix-text").textContent()) === "Couldn't change that — see the dashboard");

  // 3b. late finals: no second confirmation; a cancelled ticket can be revived and then fixed
  await sleep(2500);
  send("bug.submitted", { bugId: "bug_e2e_a", amended: true });
  await sleep(300);
  check("amended submit for a ticket already confirmed shows no second confirmation", (await page.locator(".pineo-pill-submitted").count()) === 0);
  send("bug.opened", { bugId: "bug_e2e_c" });
  await page.waitForSelector(".pineo-pill-rec");
  await tapOn('[data-pineo-id="card-show-pin"]');
  await sleep(900);
  send("bug.closing", { bugId: "bug_e2e_c" });
  await page.waitForSelector(".pineo-pill-closing");
  send("transcript", { text: "make this button", final: false });
  await sleep(4700); // the hub may hold the closing state this long
  const stillClosing = { pill: await page.locator(".pineo-pill-closing").count(), caption: await page.locator(".pineo-interim").textContent().catch(() => null) };
  send("bug.cancelled", { bugId: "bug_e2e_c", reason: "empty" });
  await page.waitForSelector(".pineo-pill-cancelled");
  check("Closing… stays up for 4.7 s with live captions until the verdict", stillClosing.pill === 1 && stillClosing.caption === "make this button", JSON.stringify(stillClosing));
  await sleep(500);
  send("bug.submitted", { bugId: "bug_e2e_c", amended: true });
  await page.waitForSelector(".pineo-pill-submitted");
  check('a cancelled ticket revived by an amended submit replaces "Closed — nothing captured" with "Ticket sent"', (await page.locator(".pineo-pill-cancelled").count()) === 0 && (await page.locator(".pineo-pill-submitted").textContent()) === "Ticket sent");
  send("bug.fix", { bugId: "bug_e2e_c", state: "working" });
  await page.waitForSelector(".pineo-fix-working");
  send("bug.fix", { bugId: "bug_e2e_c", state: "done", summary: "Show PIN button is blue" });
  await page.waitForSelector(".pineo-fix-done");
  const revivedFlash = await until(async () => (await page.locator('.pineo-picked[data-pineo-picked="card-show-pin"]').count()) === 1, 3000);
  check("bug.fix after a revive shows the Done pill and flashes the selected element", /^Done: Show PIN/.test((await page.locator(".pineo-pill-fix").textContent()) ?? "") && !!revivedFlash);
  await sleep(1500);

  // 4. a second ticket, cancelled
  send("bug.opened", { bugId: "bug_e2e_b" });
  await page.waitForSelector(".pineo-pill-rec");
  send("bug.closing", { bugId: "bug_e2e_b" });
  await page.waitForSelector(".pineo-pill-closing");
  send("bug.cancelled", { bugId: "bug_e2e_b", reason: "empty" });
  await page.waitForSelector(".pineo-pill-cancelled");
  check('cancel shows "Closed — nothing captured" and removes the ticket UI', (await page.locator(".pineo-pill-cancelled").textContent()) === "Closed — nothing captured" && (await page.locator(".pineo-pill-rec, .pineo-frame").count()) === 0);

  // 5. assist
  const assist = async (elementId, say, expectScreen) => {
    const n = received.filter((m) => m.type === "app.assist.shown").length;
    send("assist", { elementId, say });
    const reply = await until(() => received.filter((m) => m.type === "app.assist.shown")[n], 7000);
    await sleep(350);
    const info = await page.evaluate((id) => {
      const el = document.querySelector(`[data-pineo-id="${id}"]`);
      const halo = document.querySelector(".pineo-halo");
      const r = el?.getBoundingClientRect();
      const h = halo?.getBoundingClientRect();
      return {
        screen: document.querySelector(".app").getAttribute("data-screen"),
        center: r ? Math.round(r.top + r.height / 2) : null,
        vh: window.innerHeight,
        haloOnElement: !!(r && h && Math.abs(h.top - (r.top - 6)) < 3 && Math.abs(h.left - Math.max(4, r.left - 6)) < 3),
        haloAnim: halo ? getComputedStyle(halo).animationName : null,
        marker: !!document.querySelector(".pineo-marker"),
        bubble: document.querySelector(".pineo-bubble")?.textContent,
        scrollTop: Math.round(document.querySelector(".scroll").scrollTop),
      };
    }, elementId);
    const fixed = elementId.startsWith("tab-");
    const inView = info.center !== null && info.center > 80 && info.center < info.vh - (fixed ? 0 : 90);
    check(`assist → ${elementId}`,
      reply?.ok === true && info.screen === expectScreen && inView && info.haloOnElement && info.haloAnim === "pineo-halo" && info.marker && info.bubble === say,
      JSON.stringify({ reply: reply && { elementId: reply.elementId, ok: reply.ok }, ...info }));
  };
  await page.locator('[data-pineo-id="tab-home"]').tap();
  await sleep(300);
  await assist("activity-tx-refund", "Your jacket refund is here, on 27 October.", "activity");
  await shot("assist");
  await assist("transfer-submit", "The Submit transfer button is at the bottom of the form.", "transfer");
  await shot("assist-submit");
  await assist("support-call", "Call support is inside Help and support.", "profile");
  await shot("assist-collapsed");
  await assist("transfer-repeat", "Repeat appears once Schedule for later is on.", "transfer");
  await assist("card-block-toggle", "Turn on Block card to freeze this card.", "cards");
  await shot("assist-block-card");
  await assist("tab-activity", "Activity is in the bottom bar.", "cards");
  send("assist.none", { say: "I couldn't find a loans page in this app." });
  await sleep(400);
  const none = await page.evaluate(() => ({ bubble: document.querySelector(".pineo-bubble")?.textContent, halo: !!document.querySelector(".pineo-halo") }));
  check("assist.none shows the bubble only", /loans/.test(none.bubble ?? "") && !none.halo);
  const n0 = received.filter((m) => m.type === "app.assist.shown").length;
  send("assist", { elementId: "does-not-exist", say: "Hmm." });
  const bad = await until(() => received.filter((m) => m.type === "app.assist.shown")[n0], 4000);
  check("unknown element replies ok:false", bad?.ok === false);
  await sleep(6500);
  check("marker goes away by itself", (await page.locator(".pineo-halo, .pineo-bubble").count()) === 0);

  // 6. debug panel drives the hub's /api/debug routes
  await page.goto(base + "/?debug=1#/cards");
  await page.waitForSelector(".pineo-debug");
  await page.locator(".pineo-debug button", { hasText: "Open ticket" }).tap();
  const viaDebug = !!(await page.waitForSelector(".pineo-pill-rec", { timeout: 4000 }).catch(() => null));
  await shot("debug");
  await page.locator(".pineo-debug button", { hasText: "Submit" }).tap();
  await page.waitForSelector(".pineo-pill-submitted", { timeout: 4000 }).catch(() => null);
  check("debug panel (?debug=1) opens and submits a ticket through /api/debug/gesture", viaDebug);
  await page.goto(base + "/#/home");
  await sleep(300);
  for (let i = 0; i < 3; i++) await page.touchscreen.tap(120, 30);
  check("triple-tap on the header opens the debug panel", (await page.locator(".pineo-debug").count()) === 1);

  // 6b. a fix that finishes while the socket is down is picked up from hello after reconnecting
  send("bug.fix", { bugId: "bug_e2e_a", state: "working" });
  await page.waitForSelector(".pineo-fix-working");
  helloBugs = [{ id: "bug_e2e_a", status: "submitted", fix: { state: "done", summary: "Total now adds up" } }];
  for (const c of clients) c.terminate(); // the done message is "lost" here
  const resynced = await page.waitForSelector(".pineo-fix-done", { timeout: 9000 }).catch(() => null);
  check('"Working on it…" resyncs to "Done: …" from hello after a reconnect', !!resynced && /^Done: Total now adds up/.test((await page.locator(".pineo-pill-fix").textContent()) ?? ""));
  // a reload while the agent is still working shows the pill again from hello, not from storage
  helloBugs = [{ id: "bug_e2e_a", status: "submitted", fix: { state: "working" } }];
  await page.reload();
  check("after a reload the working pill comes back from the hub's state", !!(await page.waitForSelector(".pineo-fix-working", { timeout: 6000 }).catch(() => null)));
  helloBugs = [{ id: "bug_e2e_a", status: "submitted", fix: { state: "done", summary: "Total now adds up" } }];
  await page.reload();
  await sleep(1500);
  check("after a reload with the fix long finished, no stale working pill", (await page.locator(".pineo-fix-working").count()) === 0);
  helloBugs = [];
  // per-instance styling hooks exist on the shared components (no visual change by default)
  await page.goto(base + "/#/cards"); await page.waitForSelector('[data-pineo-id="card-block-toggle"]');
  check("shared rows render without inline styles unless one is passed", (await page.evaluate(() => document.querySelector('[data-pineo-id="card-change-pin"]').getAttribute("style"))) === null);
  // coming back from the background reconnects the socket
  { const before = clients.size; const firstClient = [...clients][0];
    await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true }); document.dispatchEvent(new Event("visibilitychange")); });
    await sleep(3300);
    await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true }); document.dispatchEvent(new Event("visibilitychange")); });
    const fresh = await until(() => clients.size === 1 && [...clients][0] !== firstClient, 5000);
    check("returning from the background opens a fresh socket", before === 1 && !!fresh); }

  // 7. reconnect
  for (const c of clients) c.terminate();
  await sleep(200);
  const back = await until(() => clients.size === 1, 8000);
  check("reconnects after the hub drops the socket", !!back);

  check("no console errors", consoleErrors.length === 0, consoleErrors.slice(0, 5).join(" | "));
} catch (err) {
  check("e2e run completed", false, String(err?.stack ?? err));
} finally {
  await browser.close();
  server.close();
  wss.close();
}
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
