#!/usr/bin/env node
// The whole chain with the REAL hub code, REAL MiniMax and a REAL Vite dev server, on spare
// ports and a scratch copy of mobile-app, so nothing the other workers run is touched:
//   hub (pineo/hub, own port + temp data dir)  <- debug routes drive a ticket, as the band would
//   a pretend phone (ws role=app) reports the tap/circle
//   agent/run-hub.sh edits the scratch app; Vite serves the changed module
//   node test/real-hub.mjs      exit 0 = every check passed
import { spawn } from "node:child_process";
import { cpSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AGENT_DIR, PINEO_ROOT, sleep } from "../lib/common.mjs";

const REAL = join(PINEO_ROOT, "mobile-app");
let failed = 0;
const check = (ok, name, detail = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
};

const tmp = mkdtempSync(join(tmpdir(), "pineo-real-hub-"));
const app = join(tmp, "app");
for (const f of ["src", "scripts", "package.json", "tsconfig.json", "index.html", "vite.config.ts"]) cpSync(join(REAL, f), join(app, f), { recursive: true });
symlinkSync(join(REAL, "node_modules"), join(app, "node_modules"));

const hubPort = 21000 + Math.floor(Math.random() * 9000);
const vitePort = hubPort + 1;
const H = `http://127.0.0.1:${hubPort}`;
const procs = [];
const logs = {};
function start(name, cmd, args, opts = {}) {
  const p = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"], ...opts });
  logs[name] = "";
  p.stdout.on("data", (d) => (logs[name] += d));
  p.stderr.on("data", (d) => (logs[name] += d));
  procs.push(p);
  return p;
}
const post = async (path, body) => (await fetch(`${H}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })).json();
const until = async (fn, ms = 20000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try {
      const v = await fn();
      if (v) return v;
    } catch {}
    await sleep(100);
  }
  return undefined;
};

try {
  start("agent", "sh", [join(AGENT_DIR, "run-hub.sh"), "--app", app, "--hub", `ws://127.0.0.1:${hubPort}/ws?role=agent`]); // before the hub is up
  start("hub", join(PINEO_ROOT, ".venv/bin/python"), ["-m", "hub", "--host", "127.0.0.1", "--port", String(hubPort)], { cwd: PINEO_ROOT, env: { ...process.env, PINEO_HUB_DATA: join(tmp, "hub-data") } });
  start("vite", join(app, "node_modules/.bin/vite"), ["--port", String(vitePort), "--host", "127.0.0.1"], { cwd: app });
  check(await until(async () => (await fetch(`${H}/api/state`)).ok), "real hub is up on a spare port", `:${hubPort}`);
  check(await until(async () => (await fetch(`http://127.0.0.1:${vitePort}/src/screens/Transfer.tsx`)).ok), "Vite dev server serves the scratch app", `:${vitePort}`);
  check(await until(() => logs.agent.includes("connected to the hub")), "agent connected to the real hub as role=agent");

  // The pretend phone.
  const phone = new WebSocket(`ws://127.0.0.1:${hubPort}/ws?role=app`);
  const phoneGot = [];
  phone.addEventListener("message", (e) => phoneGot.push({ at: Date.now(), ...JSON.parse(e.data) }));
  await new Promise((r) => phone.addEventListener("open", r));
  const tell = (msg) => phone.send(JSON.stringify({ ts: Date.now(), ...msg }));

  async function ticket({ screen, say, items }) {
    tell({ type: "app.context", screen, visibleElementIds: items.flatMap((i) => i.elementIds ?? [i.elementId]), viewport: { width: 390, height: 844 } });
    await sleep(150);
    const opened = await post("/api/debug/gesture", { name: "open" });
    const bugId = opened.bug?.id;
    await sleep(100);
    tell({ type: "app.interaction", bugId, items: items.map((i) => ({ at: Date.now(), screen, ...i })) });
    await post("/api/debug/transcript", { text: say, final: true });
    await sleep(150);
    const t0 = Date.now();
    await post("/api/debug/gesture", { name: "submit" });
    const bug = await until(async () => {
      const b = await (await fetch(`${H}/api/bugs/${bugId}`)).json();
      const fix = (b.bug ?? b).fix;
      return fix && (fix.state === "done" || fix.state === "failed") ? (b.bug ?? b) : undefined;
    }, 150000);
    const ms = Date.now() - t0;
    const toPhone = phoneGot.filter((m) => m.type === "bug.fix" && m.bugId === bugId).map((m) => `${m.state}@${m.at - t0}ms`);
    return { bugId, bug, ms, toPhone };
  }
  const served = async (path) => (await fetch(`http://127.0.0.1:${vitePort}${path}`)).text();

  const a = await ticket({ screen: "transfer", say: "change the text on this button to Send money", items: [{ kind: "tap", elementId: "transfer-submit", label: "Submit transfer" }] });
  console.log(`\n1. submit -> done ${a.ms} ms; fix ${JSON.stringify(a.bug?.fix)}; phone saw ${a.toPhone.join(", ")}`);
  check(a.bug?.fix?.state === "done", "1 text change: the hub stored fix.state done");
  check(a.toPhone.some((s) => s.startsWith("working")) && a.toPhone.some((s) => s.startsWith("done")), "1 the phone was told working, then done");
  check(await until(async () => /Send money\s*\n?\s*<\/button>|children: "Send money"|"Send money"/.test(await served("/src/screens/Transfer.tsx")), 5000), "1 Vite now serves Transfer.tsx with the new text (what hot reload delivers)");

  const b = await ticket({ screen: "transfer", say: "change the color of this button", items: [{ kind: "tap", elementId: "transfer-cancel" }, { kind: "tap", elementId: "transfer-submit" }] });
  console.log(`\n2. submit -> done ${b.ms} ms; fix ${JSON.stringify(b.bug?.fix)}; phone saw ${b.toPhone.join(", ")}`);
  check(b.bug?.fix?.state === "done", "2 colour change: done");

  const c = await ticket({ screen: "home", say: "make this card blue", items: [{ kind: "circle", elementIds: ["home-balance"], path: [[0.1, 0.2], [0.9, 0.2], [0.9, 0.4], [0.1, 0.4]] }] });
  console.log(`\n3. submit -> done ${c.ms} ms; fix ${JSON.stringify(c.bug?.fix)}; phone saw ${c.toPhone.join(", ")}`);
  check(c.bug?.fix?.state === "done", "3 circled card blue: done");
  const home = await served("/src/screens/Home.tsx");
  const css = await served("/src/styles.css");
  check(!/Internal server error|plugin:vite/.test(home + css + (await served("/src/screens/Transfer.tsx"))), "Vite reports no transform error for the edited files");
  check(!/error/i.test(logs.vite.replace(/\x1b\[[0-9;]*m/g, "")), "Vite's own log has no error");

  console.log("\nLATENCY (submit gesture -> fix done stored by the hub, real MiniMax, real hub):");
  console.log(`  text change    ${a.ms} ms\n  colour change  ${b.ms} ms\n  card blue      ${c.ms} ms`);
  phone.close();
} catch (error) {
  check(false, "test crashed", String(error.stack ?? error));
} finally {
  for (const p of procs) p.kill("SIGTERM");
  await sleep(300);
  for (const name of ["agent", "vite"]) console.log(`\n--- ${name} log ---\n${logs[name].trimEnd().replace(/\x1b\[[0-9;]*m/g, "").replaceAll(tmp, "<tmp>")}`);
  rmSync(tmp, { recursive: true, force: true });
}
console.log(failed === 0 ? "\nREAL HUB CHAIN OK" : `\nREAL HUB CHAIN FAILED: ${failed} check(s)`);
process.exit(failed === 0 ? 0 : 1);
