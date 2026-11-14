#!/usr/bin/env node
// End-to-end check of hub-runner.mjs on REAL MiniMax, with a stand-in hub and a scratch copy
// of mobile-app (the real app and the real hub are not touched).
//   node test/hub-e2e.mjs [--no-fast]      exit 0 = every check passed
import { spawn, execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs";
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

const app = mkdtempSync(join(tmpdir(), "pineo-hub-e2e-"));
for (const f of ["src", "scripts", "package.json", "tsconfig.json", "index.html", "vite.config.ts"]) cpSync(join(REAL, f), join(app, f), { recursive: true });
symlinkSync(join(REAL, "node_modules"), join(app, "node_modules"));
const read = (rel) => readFileSync(join(app, rel), "utf8");
const compiles = () => {
  try {
    execFileSync(join(app, "node_modules/.bin/tsc"), ["--noEmit"], { cwd: app, stdio: "pipe" });
    execFileSync(process.execPath, ["scripts/kb-check.mjs"], { cwd: app, stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
};

// The runner starts BEFORE the hub exists: it must keep retrying.
const port = 20000 + Math.floor(Math.random() * 20000);
const runner = spawn("sh", [join(AGENT_DIR, "run-hub.sh"), "--app", app, "--hub", `ws://127.0.0.1:${port}/ws?role=agent`, ...process.argv.slice(2)], { stdio: ["ignore", "pipe", "pipe"] });
let log = "";
runner.stdout.on("data", (d) => {
  log += d;
  if (process.env.PINEO_TEST_VERBOSE) process.stderr.write(d);
});
runner.stderr.on("data", (d) => (log += d));
await sleep(1500);

const fixes = []; // every bug.fix the agent sent, with the time it arrived
let agent;
const wss = new WebSocketServer({ port, host: "127.0.0.1" });
const connected = new Promise((done) =>
  wss.on("connection", (sock, req) => {
    agent = sock;
    sock.send(JSON.stringify({ type: "hello", ts: Date.now(), role: "agent", state: { bugs: [] } }));
    sock.on("message", (d) => fixes.push({ at: Date.now(), ...JSON.parse(String(d)) }));
    done(req.url);
  }),
);
const url = await Promise.race([connected, sleep(8000).then(() => null)]);
check(url === "/ws?role=agent", "runner connects once the hub appears (started 1.5 s before it)", String(url));

let n = 0;
function ticket({ say, tap, taps, circle, screen }) {
  const id = `bug_test${++n}`;
  const now = Date.now();
  const interactions = [{ at: now - 3000, kind: "screen", screen }];
  for (const t of taps ?? []) interactions.push({ at: now - 2500, kind: "tap", screen, ...t });
  if (tap) interactions.push({ at: now - 2000, kind: "tap", screen, elementId: tap, selected: true });
  if (circle) interactions.push({ at: now - 1500, kind: "circle", screen, elementIds: circle, path: [[0.2, 0.5], [0.8, 0.5], [0.8, 0.6], [0.2, 0.6]] });
  return {
    id, status: "submitted", kind: "", openedAt: now - 4000, submittedAt: now, screen,
    transcript: [{ text: say, at: now - 1000 }], interactions, elementIds: [],
    contextAtOpen: { screen, visibleElementIds: [tap, ...(circle ?? [])].filter(Boolean) },
    title: "", summary: "", severity: "", steps: [],
  };
}
async function submit(spec) {
  const bug = ticket(spec);
  const sentAt = Date.now();
  agent.send(JSON.stringify({ type: "bug.submitted", ts: sentAt, bug }));
  for (;;) {
    const end = fixes.find((f) => f.bugId === bug.id && (f.state === "done" || f.state === "failed"));
    if (end) {
      const working = fixes.find((f) => f.bugId === bug.id && f.state === "working");
      return { ...end, wallMs: end.at - sentAt, workingAfterMs: working ? working.at - sentAt : null };
    }
    if (Date.now() - sentAt > 150000) return { state: "timeout", wallMs: Date.now() - sentAt };
    await sleep(25);
  }
}
const show = (r) => `${r.state} in ${r.wallMs} ms (agent says ${r.ms} ms); working after ${r.workingAfterMs} ms; "${r.summary}"; files ${JSON.stringify(r.files)}`;

try {
  // 1. text change, tapped element
  const tsxBefore = read("src/screens/Transfer.tsx");
  const a = await submit({ screen: "transfer", tap: "transfer-submit", say: "change the text on this button to Send money" });
  console.log(`\n1. ${show(a)}`);
  check(a.state === "done", "1 text change: done");
  check(a.workingAfterMs !== null && a.workingAfterMs < 500, "1 'working' was sent at once", `${a.workingAfterMs} ms`);
  const tsx = read("src/screens/Transfer.tsx");
  check(/data-pineo-id="transfer-submit"[^>]*>\s*Send money\s*</.test(tsx) && !tsx.includes("Submit transfer\n"), "1 the button now reads Send money", "src/screens/Transfer.tsx");
  check(read("src/pineo/elements.json") === readFileSync(join(REAL, "src/pineo/elements.json"), "utf8") && !(a.files ?? []).some((f) => f.startsWith("src/pineo/")), "1 elements.json (client code) was not touched, so the phone hot-swaps instead of reloading");
  check((a.summary ?? "").length <= 70, "1 the summary fits the phone (70 characters at most)", `${(a.summary ?? "").length}: ${a.summary}`);
  check(compiles(), "1 app still compiles (tsc + kb-check)");
  check(tsx.split("\n").length === tsxBefore.split("\n").length, "1 edit is minimal (same number of lines)");

  // 2. colour change with no colour named
  const b = await submit({ screen: "transfer", tap: "transfer-submit", say: "change the color of this button" });
  console.log(`\n2. ${show(b)}`);
  check(b.state === "done", "2 colour change: done");
  check(compiles(), "2 app still compiles");
  check((b.files ?? []).length >= 1 && (b.files ?? []).every((f) => f.startsWith("src/") && (!f.startsWith("src/pineo/") || f === "src/pineo/elements.json")), "2 only app source was edited", JSON.stringify(b.files));

  // 3. "make this card blue", circled (the circle wins over an earlier tap)
  const c = await submit({ screen: "home", tap: "home-quick-send", circle: ["home-balance"], say: "make this card blue" });
  console.log(`\n3. ${show(c)}`);
  check(c.state === "done", "3 circled card: done");
  check(compiles(), "3 app still compiles");
  const home = read("src/screens/Home.tsx") + read("src/styles.css");
  check(/blue|#[0-9a-f]{3,8}|rgb/i.test(c.summary + home) && (c.files ?? []).some((f) => /Home\.(tsx|css)|styles\.css/.test(f)), "3 the edit is in Home.tsx, Home.css or styles.css", JSON.stringify(c.files));

  // 4. not a change request -> failed, nothing edited
  const before4 = read("src/screens/Home.tsx") + read("src/styles.css") + read("src/screens/Transfer.tsx");
  const d = await submit({ screen: "home", tap: "home-balance", say: "why does this number look wrong sometimes" });
  console.log(`\n4. ${show(d)}`);
  check(d.state === "failed" && (d.files ?? []).length === 0, "4 a question is answered failed with a reason, not guessed at");
  check(read("src/screens/Home.tsx") + read("src/styles.css") + read("src/screens/Transfer.tsx") === before4, "4 nothing was edited");

  // 4b. a remark with no stated fix (not a wh-question, so the model must decide) -> failed
  const d2 = await submit({ screen: "home", tap: "home-balance", say: "this number looks wrong to me sometimes" });
  console.log(`\n4b. ${show(d2)}`);
  check(d2.state === "failed" && (d2.files ?? []).length === 0, "4b a vague remark is answered failed, not guessed at");
  check(read("src/screens/Home.tsx") + read("src/styles.css") + read("src/screens/Transfer.tsx") === before4, "4b nothing was edited");

  // 4c. several elements selected, one deselected again: both remaining ones change, the deselected one does not
  const tBefore = read("src/screens/Transfer.tsx");
  const lineOf = (text, id) => text.split("\n").findIndex((l) => l.includes(`data-pineo-id="${id}"`));
  const m = await submit({ screen: "transfer", say: "make these two buttons the same colour, dark green with white text", taps: [
    { elementId: "transfer-submit", selected: true }, { elementId: "transfer-confirm-checkbox", selected: true },
    { elementId: "transfer-cancel", selected: true }, { elementId: "transfer-confirm-checkbox", selected: false }] });
  console.log(`\n4c. ${show(m)}`);
  const tAfter = read("src/screens/Transfer.tsx");
  const changed = (id) => tBefore.split("\n")[lineOf(tBefore, id)] !== tAfter.split("\n")[lineOf(tAfter, id)];
  check(m.state === "done", "4c two selected buttons: done");
  check(changed("transfer-submit") && changed("transfer-cancel"), "4c both selected buttons were edited");
  check(!changed("transfer-confirm-checkbox"), "4c the deselected element was left alone");
  check(compiles(), "4c app still compiles");

  // 5. two tickets at once: handled in order, the second is queued
  const before5 = fixes.length;
  const [e, f] = await Promise.all([
    submit({ screen: "home", tap: "home-goal-add", say: "change this button text to Top up goal" }),
    submit({ screen: "profile", tap: "profile-logout", say: "make this button red" }),
  ]);
  console.log(`\n5a. ${show(e)}\n5b. ${show(f)}`);
  const order = fixes.slice(before5).map((x) => `${x.bugId.replace("bug_test", "#")}:${x.state}`).join(" ");
  check(e.state === "done" && f.state === "done", "5 both tickets done", order);
  check(/#7:working #8:queued #7:done #8:working #8:done/.test(order), "5 one at a time, in order, second one queued");
  check(compiles(), "5 app still compiles");

  // 6. late finals: the hub re-sends the ticket with more of the sentence (amended: true)
  const finals = (id) => fixes.filter((x) => x.bugId === id && (x.state === "done" || x.state === "failed"));
  async function amendedTicket({ first, rest, afterMs, select, screen, firstIsAmended = false }) {
    const bug = ticket({ say: first, tap: select, screen });
    const sentAt = Date.now();
    agent.send(JSON.stringify({ type: "bug.submitted", ts: sentAt, bug, ...(firstIsAmended ? { amended: true } : {}) }));
    if (rest) {
      await sleep(afterMs);
      const fuller = { ...bug, amended: true, transcript: [...bug.transcript, { text: rest, at: Date.now(), amended: true }] };
      agent.send(JSON.stringify({ type: "bug.submitted", ts: Date.now(), bugId: bug.id, bug: fuller, amended: true }));
    }
    for (;;) {
      const end = finals(bug.id)[0];
      if (end) {
        await sleep(400); // a second answer for the same ticket would show up here
        return { ...end, id: bug.id, wallMs: end.at - sentAt, answers: finals(bug.id).length };
      }
      if (Date.now() - sentAt > 150000) return { state: "timeout", id: bug.id, wallMs: Date.now() - sentAt, answers: 0 };
      await sleep(25);
    }
  }
  const showA = (r) => `${r.state} in ${r.wallMs} ms; "${r.summary}"; files ${JSON.stringify(r.files)}; answers ${r.answers}`;
  const profileLine = () => read("src/screens/Profile.tsx").split("\n").find((l) => l.includes('data-pineo-id="profile-edit"'));

  // 6a. the rest arrives 700 ms later, while the first half is being worked on
  const p0 = profileLine();
  const g = await amendedTicket({ screen: "profile", select: "profile-edit", first: "I want this button", rest: "to be dark green with white text.", afterMs: 700 });
  console.log(`\n6a. ${showA(g)}`);
  check(g.state === "done" && g.answers === 1, "6a amended while in progress: one answer, done", `answers ${g.answers}`);
  check(profileLine() !== p0 && /green|#0|#1|rgb/i.test(`${g.summary} ${profileLine()}`), "6a the edit follows the full sentence (green)", profileLine()?.trim().slice(0, 170));
  check(compiles(), "6a app still compiles");

  // 6b. a half sentence and nothing more: waits briefly, then a short human "failed", nothing edited
  const before6b = read("src/screens/Profile.tsx");
  const h = await amendedTicket({ screen: "profile", select: "profile-logout", first: "I want this button to" });
  console.log(`\n6b. ${showA(h)}`);
  check(h.state === "failed" && h.answers === 1 && read("src/screens/Profile.tsx") === before6b, "6b cut-off sentence, never completed: failed, nothing edited");
  check(h.wallMs >= 2000 && h.wallMs < 9000, "6b it waited for the rest before giving up", `${h.wallMs} ms`);
  check((h.summary ?? "").length > 0 && h.summary.length <= 90, "6b the failed message is short", `${(h.summary ?? "").length} chars`);

  // 6c. the rest arrives while the runner is holding back a "failed"
  const i = await amendedTicket({ screen: "profile", select: "profile-logout", first: "I want this button to", rest: "say Sign out.", afterMs: 1500 });
  console.log(`\n6c. ${showA(i)}`);
  check(i.state === "done" && i.answers === 1 && /Sign out/.test(read("src/screens/Profile.tsx")), "6c the rest arrived during the wait: done with the full sentence, one answer");

  // 6d. a ticket the hub had cancelled as empty arrives for the first time as amended: a normal ticket
  const j = await amendedTicket({ screen: "home", select: "home-goal-add", first: "change this button text to Save more", firstIsAmended: true });
  console.log(`\n6d. ${showA(j)}`);
  check(j.state === "done" && j.answers === 1 && /Save more/.test(read("src/screens/Home.tsx")), "6d first copy already marked amended: handled as a new ticket");
  check(compiles(), "6 app still compiles");

  console.log("\nLATENCY (ticket submitted -> bug.fix done, real MiniMax):");
  for (const [name, r] of [["text change", a], ["colour change", b], ["card blue (circle)", c], ["text change 2", e]]) console.log(`  ${name.padEnd(20)} ${r.wallMs} ms`);
} catch (error) {
  check(false, "test crashed", String(error.stack ?? error));
} finally {
  runner.kill("SIGTERM");
  wss.close();
  console.log("\n--- git-style summary of what changed in the scratch app ---");
  try {
    console.log(execFileSync("diff", ["-ru", join(REAL, "src"), join(app, "src")], { encoding: "utf8" }));
  } catch (e) {
    console.log(String(e.stdout ?? "").replaceAll(app, "<scratch>").replaceAll(REAL, "mobile-app"));
  }
  console.log("--- runner log ---\n" + log.trimEnd().replaceAll(app, "<scratch>"));
  rmSync(app, { recursive: true, force: true });
}
console.log(failed === 0 ? "\nHUB E2E OK" : `\nHUB E2E FAILED: ${failed} check(s)`);
process.exit(failed === 0 ? 0 : 1);
