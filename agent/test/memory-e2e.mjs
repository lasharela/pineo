#!/usr/bin/env node
// Memory, undo and the no-selection fast path of hub-runner.mjs, on REAL MiniMax, with a
// stand-in hub, a scratch copy of mobile-app and a temp state dir (the real app, the real hub
// and agent/.state are not touched).
//   node test/memory-e2e.mjs      exit 0 = every check passed
import { spawn, execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AGENT_DIR, PINEO_ROOT, sleep } from "../lib/common.mjs";

const REAL = join(PINEO_ROOT, "mobile-app");
const { WebSocketServer } = createRequire(join(REAL, "package.json"))("ws");

let failed = 0;
const check = (ok, name, detail = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
};

const tmp = mkdtempSync(join(tmpdir(), "pineo-memory-e2e-"));
const app = join(tmp, "app");
for (const f of ["src", "scripts", "package.json", "tsconfig.json", "index.html", "vite.config.ts"]) cpSync(join(REAL, f), join(app, f), { recursive: true });
symlinkSync(join(REAL, "node_modules"), join(app, "node_modules"));
const read = (rel) => readFileSync(join(app, rel), "utf8");
/** Every source file's content in one string, to compare whole states. */
const state = () => {
  const out = [];
  (function walk(dir) {
    for (const d of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const p = join(dir, d.name);
      if (d.isDirectory()) walk(p);
      else out.push(`${p}\n${readFileSync(p, "utf8")}`);
    }
  })(join(app, "src"));
  return out.join("\n\u0000");
};
const compiles = () => {
  try {
    execFileSync(join(app, "node_modules/.bin/tsc"), ["--noEmit"], { cwd: app, stdio: "pipe" });
    execFileSync(process.execPath, ["scripts/kb-check.mjs"], { cwd: app, stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
};

const port = 20000 + Math.floor(Math.random() * 20000);
let log = "";
let runner;
function startRunner() {
  runner = spawn("sh", [join(AGENT_DIR, "run-hub.sh"), "--app", app, "--hub", `ws://127.0.0.1:${port}/ws?role=agent`], {
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PINEO_AGENT_STATE: join(tmp, "state") },
  });
  runner.stdout.on("data", (d) => {
    log += d;
    if (process.env.PINEO_TEST_VERBOSE) process.stderr.write(d);
  });
  runner.stderr.on("data", (d) => (log += d));
}

const fixes = [];
let agent;
let onConnect = () => {};
const wss = new WebSocketServer({ port, host: "127.0.0.1" });
wss.on("connection", (sock) => {
  agent = sock;
  sock.send(JSON.stringify({ type: "hello", ts: Date.now(), role: "agent", state: { bugs: [] } }));
  sock.on("message", (d) => fixes.push({ at: Date.now(), ...JSON.parse(String(d)) }));
  onConnect();
});
const connected = () => new Promise((done) => (onConnect = done));

let n = 0;
async function say(text, { screen = "transfer", select = [], visible = [] } = {}) {
  const id = `bug_mem${++n}`;
  const now = Date.now();
  const bug = {
    id, status: "submitted", kind: "", openedAt: now - 4000, submittedAt: now, screen,
    transcript: [{ text, at: now - 1000 }],
    interactions: select.map((elementId) => ({ at: now - 2000, kind: "tap", screen, elementId, selected: true })),
    elementIds: [...select], contextAtOpen: { screen, visibleElementIds: [...select, ...visible] },
    title: "", summary: "", severity: "", steps: [],
  };
  const sentAt = Date.now();
  agent.send(JSON.stringify({ type: "bug.submitted", ts: sentAt, bug }));
  for (;;) {
    const end = fixes.find((f) => f.bugId === id && (f.state === "done" || f.state === "failed"));
    if (end) {
      await sleep(30);
      const line = log.split("\n").find((l) => l.includes(`ticket ${id}:`) && / via /.test(l)) ?? "";
      const r = { ...end, wallMs: end.at - sentAt, path: / via (\S+)/.exec(line)?.[1] ?? "?", steps: /\{(.*)\}\s*$/.exec(line)?.[1] ?? "", progress: fixes.filter((f) => f.bugId === id && f.state === "working" && f.summary).map((f) => f.summary) };
      console.log(`\n${n}. "${text}"${select.length ? ` [selected ${select.join(", ")}]` : ` [nothing selected, screen ${screen}]`}\n   -> ${r.state} in ${r.wallMs} ms via ${r.path}: "${r.summary}" files ${JSON.stringify(r.files)}\n   steps: ${r.steps}`);
      return r;
    }
    if (Date.now() - sentAt > 150000) return { state: "timeout", wallMs: Date.now() - sentAt, path: "?" };
    await sleep(25);
  }
}
const times = [];
const note = (name, r) => times.push(`  ${name.padEnd(46)} ${String(r.wallMs).padStart(6)} ms  via ${r.path}`);

try {
  startRunner();
  await Promise.race([connected(), sleep(8000)]);
  check(Boolean(agent), "runner connected");
  const s0 = state();
  const submitLine = () => read("src/screens/Transfer.tsx").split("\n").find((l) => l.includes('data-pineo-id="transfer-submit"'));

  // A. change, then a plain "turn it back": deterministic, no model.
  const a1 = await say("make this button purple", { select: ["transfer-submit"] });
  check(a1.state === "done" && state() !== s0, "A change: done");
  const a2 = await say("turn it back");
  check(a2.state === "done" && a2.path === "undo", "A 'turn it back' is done by the deterministic undo (no model call)", `via ${a2.path}`);
  check(state() === s0, "A every source file is byte-for-byte as before the change");
  check(/^Reverted/.test(a2.summary ?? ""), "A summary says what was reverted", a2.summary);
  note("change (selected button)", a1);
  note("'turn it back' (deterministic undo)", a2);

  // B. change, then a follow-up with nothing selected that only makes sense with memory.
  const b1 = await say("make this button blue", { select: ["transfer-submit"] });
  const afterBlue = submitLine();
  const b2 = await say("make it darker");
  check(b1.state === "done" && b2.state === "done", "B change + 'make it darker': both done");
  check(submitLine() !== afterBlue && (b2.files ?? []).includes("src/screens/Transfer.tsx"), "B 'make it darker' changed the same button again", submitLine()?.trim().slice(0, 150));
  check(compiles(), "B app still compiles");
  note("'make it darker' (follow-up, nothing selected)", b2);

  // C. "undo the last two": both changes of B, in order.
  const c = await say("undo the last two");
  check(c.state === "done" && c.path === "undo" && state() === s0, "C 'undo the last two' restores the state before B, without the model", `via ${c.path}`);
  note("'undo the last two'", c);

  // D. nothing selected, screen-level request: must go through the fast path.
  const cardsBefore = state();
  const d = await say("make the switches purple", { screen: "cards", visible: ["card-block-toggle", "card-online-toggle", "card-contactless-toggle", "card-atm-toggle", "card-abroad-toggle", "cards-carousel"] });
  check(d.state === "done" && d.path === "fast", "D unselected 'make the switches purple' on Cards: done by the fast path", `via ${d.path}; ${d.steps}`);
  check(compiles() && state() !== cardsBefore, "D something changed and the app compiles", JSON.stringify(d.files));
  note("'make the switches purple' (nothing selected)", d);

  // E. an undo in other words: the model names the change, the runner restores it.
  const e = await say("turn colors back", { screen: "cards" });
  check(e.state === "done" && state() === cardsBefore, "E 'turn colors back' restores the files exactly", `via ${e.path}`);
  check(e.path === "fast-undo", "E done by the model naming the change + deterministic restore", `via ${e.path}`);
  note("'turn colors back' (model picks, runner restores)", e);

  // F. memory survives a runner restart.
  const f1 = await say("change the text on this button to Pay now", { select: ["transfer-submit"] });
  check(f1.state === "done" && /Pay now/.test(read("src/screens/Transfer.tsx")), "F change before the restart: done");
  runner.kill("SIGTERM");
  await sleep(300);
  agent = undefined;
  startRunner();
  await Promise.race([connected(), sleep(8000)]);
  const f2 = await say("undo that");
  check(f2.state === "done" && f2.path === "undo" && state() === s0, "F after a runner restart 'undo that' still restores it", `via ${f2.path}`);
  note("'undo that' after a runner restart", f2);

  // G. nothing left to undo.
  const g = await say("undo");
  check(g.state === "failed" && (g.files ?? []).length === 0 && state() === s0, "G 'undo' with nothing left is answered failed, nothing touched", g.summary);

  // H. the file was changed by someone else since: no blind restore, the model puts the old lines back.
  const h1 = await say("make this button red", { select: ["transfer-cancel"] });
  const t = read("src/screens/Transfer.tsx");
  writeFileSync(join(app, "src/screens/Transfer.tsx"), t.replace("const FEE_INSTANT = 1.5;", "const FEE_INSTANT = 1.5; // edited by someone else"));
  const h2 = await say("undo that");
  const after = read("src/screens/Transfer.tsx");
  const cancelLine = (text) => text.split("\n").find((l) => l.includes('data-pineo-id="transfer-cancel"'));
  check(h1.state === "done" && h2.state === "done" && h2.path === "undo", "H undo when the file was also edited by someone else: still deterministic (only the changed lines are taken back)", `via ${h2.path}; ${h2.steps}`);
  check(after.includes("// edited by someone else"), "H the other person's edit is kept");
  check(cancelLine(after) === cancelLine(readFileSync(join(REAL, "src/screens/Transfer.tsx"), "utf8")), "H the button line is exactly as it was", String(cancelLine(after)).trim().slice(0, 160));

  // I. the changed lines themselves were edited by someone else: the runner cannot restore, the model does.
  const i1 = await say("make this button orange", { select: ["transfer-cancel"] });
  const t2 = read("src/screens/Transfer.tsx");
  const line = cancelLine(t2);
  writeFileSync(join(app, "src/screens/Transfer.tsx"), t2.replace(line, line.replace('type="button"', 'type="button" title="x"')));
  const i2 = await say("undo that");
  const after2 = read("src/screens/Transfer.tsx");
  check(i1.state === "done" && i2.state === "done" && i2.path !== "undo", "I undo when the changed line itself was edited since: handed to the model with the diff", `via ${i2.path}; ${i2.steps}`);
  check(cancelLine(after2).includes('title="x"') && !/orange|#f[0-9a-f]{2,5}\b/i.test(cancelLine(after2)) , "I the colour is gone and the other edit is kept", String(cancelLine(after2)).trim().slice(0, 160));
  note("'undo that', changed line edited since (model)", i2);
  check(compiles(), "H app still compiles");
  note("'undo that', file also edited elsewhere", h2);

  // J. nothing selected, the request is about what the app SAYS: found by its text in the source (data file).
  const user = () => /USER = \{[\s\S]*?\}/.exec(read("src/data.ts"))?.[0] ?? "";
  const oldFirst = /first: "([^"]+)"/.exec(user())?.[1];
  const j1 = await say("My name is l a s h a, Lasha. Not", { screen: "activity", visible: ["activity-search", "activity-dispute"] });
  check(j1.state === "done" && /first: "Lasha"/.test(user()), "J cut-off 'My name is l a s h a, Lasha. Not' on the wrong screen: the displayed name is now Lasha", `was ${oldFirst}; ${user().split("\n").slice(1, 3).join(" ").trim()}`);
  check(compiles(), "J app still compiles");
  note("'My name is l a s h a, Lasha. Not' (cut off)", j1);
  const j1u = await say("undo that", { screen: "activity" });
  check(j1u.state === "done" && new RegExp(`first: "${oldFirst}"`).test(user()), "J and 'undo that' puts the old name back");
  const j2 = await say(`My name is Lasha, not ${oldFirst}`, { screen: "home", visible: ["home-balance"] });
  check(j2.state === "done" && /first: "Lasha"/.test(user()), "J 'My name is Lasha, not <current name>': name changed");
  note("'My name is Lasha, not <current name>'", j2);
  const j3 = await say("change the greeting to Welcome back", { screen: "home", visible: ["home-balance"] });
  check(j3.state === "done" && /Welcome back/.test(read("src/screens/Home.tsx")) && !/Good morning/.test(read("src/screens/Home.tsx")), "J 'change the greeting to Welcome back': the greeting line changed");
  note("'change the greeting to Welcome back'", j3);
  const j4 = await say("the rent payment should be 1,450 dollars", { screen: "activity", visible: ["activity-search"] });
  check(j4.state === "done" && /Rent · November", "bill", -1450/.test(read("src/data.ts")), "J 'the rent payment should be 1,450 dollars': the rent row in the data file is 1450", read("src/data.ts").split("\n").find((l) => l.includes("Rent · November"))?.trim().slice(0, 110));
  check(compiles(), "J app still compiles after all four");
  note("'the rent payment should be 1,450 dollars'", j4);

  console.log("\nTIMINGS (ticket submitted -> bug.fix, real MiniMax):\n" + times.join("\n"));
} catch (error) {
  check(false, "test crashed", String(error.stack ?? error));
} finally {
  runner?.kill("SIGTERM");
  wss.close();
  console.log("\n--- runner log ---\n" + log.trimEnd().replaceAll(tmp, "<tmp>"));
  rmSync(tmp, { recursive: true, force: true });
}
console.log(failed === 0 ? "\nMEMORY E2E OK" : `\nMEMORY E2E FAILED: ${failed} check(s)`);
process.exit(failed === 0 ? 0 : 1);
