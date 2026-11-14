#!/usr/bin/env node
// Checks for the review items (docs/reviews/agent-review.md): no model needed except where said.
// Uses a scratch copy of mobile-app and a temp state dir; calls fixTicket directly.
//   node test/review-e2e.mjs      exit 0 = every check passed
import { execFileSync, spawn } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AGENT_DIR, PINEO_ROOT } from "../lib/common.mjs";

const REAL = join(PINEO_ROOT, "mobile-app");
let failed = 0;
const check = (ok, name, detail = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
};
const tmp = mkdtempSync(join(tmpdir(), "pineo-review-e2e-"));
const app = join(tmp, "app");
for (const f of ["src", "scripts", "package.json", "tsconfig.json", "index.html", "vite.config.ts"]) cpSync(join(REAL, f), join(app, f), { recursive: true });
symlinkSync(join(REAL, "node_modules"), join(app, "node_modules"));

/** Run one or more tickets through fixTicket in a child process with its own environment. */
function tickets(list, env = {}) {
  const code = `
    process.argv[1] = "x";
    const m = await import(${JSON.stringify(join(AGENT_DIR, "hub-runner.mjs"))});
    const out = [];
    for (const bug of ${JSON.stringify(list)}) {
      const t0 = Date.now();
      const r = await m.fixTicket(bug);
      out.push({ ...r, ms: Date.now() - t0 });
    }
    console.log("RESULTS " + JSON.stringify(out));`;
  const stdout = execFileSync(process.execPath, ["--input-type=module", "-e", code], {
    encoding: "utf8", env: { ...process.env, PINEO_APP_DIR: app, PINEO_AGENT_STATE: join(tmp, "state"), ...env }, timeout: 240000,
  });
  return { results: JSON.parse(/^RESULTS (.*)$/m.exec(stdout)[1]), log: stdout };
}
const bug = (id, text, extra = {}) => ({ id, status: "submitted", screen: "transfer", transcript: [{ text, at: Date.now() }], interactions: [], elementIds: [], contextAtOpen: { screen: "transfer", visibleElementIds: ["transfer-submit", "transfer-cancel"] }, ...extra });
const tap = (id) => ({ at: Date.now(), kind: "tap", screen: "transfer", elementId: id, selected: true });
const mtimes = () => {
  const out = [];
  (function walk(dir) {
    for (const d of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, d.name);
      if (d.isDirectory()) walk(p);
      else out.push(`${p}:${statSync(p).mtimeMs}:${statSync(p).size}`);
    }
  })(join(app, "src"));
  return out.sort().join("\n");
};
const T = "src/screens/Transfer.tsx";
const submit = () => readFileSync(join(app, T), "utf8").split("\n").find((l) => l.includes('data-pineo-id="transfer-submit"'));
const fake = (edits, summary = "x") => ({ PINEO_FAKE_FAST_ANSWER: JSON.stringify({ ok: true, summary, edits }), PINEO_NO_FALLBACK: "1" });

try {
  // (2) an old tap is not a selection
  {
    const m = await import(join(AGENT_DIR, "hub-runner.mjs") + "?x").catch(() => null);
    const code = `process.argv[1]="x"; const m = await import(${JSON.stringify(join(AGENT_DIR, "hub-runner.mjs"))});
      console.log(JSON.stringify([m.targetIds({ interactions: [], elementIds: ["home-balance"], contextAtOpen: { lastTappedElementId: "tab-cards" } }),
        m.targetIds({ interactions: [{ kind: "tap", elementId: "a", selected: true }, { kind: "tap", elementId: "a", selected: false }], elementIds: ["a"], contextAtOpen: { lastTappedElementId: "a" } }),
        m.targetIds({ interactions: [{ kind: "tap", elementId: "b", selected: true }], contextAtOpen: { lastTappedElementId: "tab-home" } })]));`;
    const out = JSON.parse(execFileSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8", env: { ...process.env, PINEO_APP_DIR: app, PINEO_AGENT_STATE: join(tmp, "state") } }).trim().split("\n").pop());
    check(out[0].length === 0 && out[1].length === 0 && out[2].join() === "b", "(2) lastTappedElementId and hub-seeded elementIds are never the selection", JSON.stringify(out));
  }

  // (4) a broken edit never reaches the real src
  {
    const before = mtimes();
    const line = submit();
    const r = tickets([bug("bug_r1", "make this button green", { interactions: [tap("transfer-submit")] })], fake([{ file: T, search: line, replace: line.replace("<button", "<buton") }])).results[0];
    check(r.state === "failed" && mtimes() === before, "(4) JSX syntax error: failed, and no file in the real src was written at all (mtimes unchanged)", `${r.summary}; ${r.steps.join("; ").slice(0, 160)}`);
    const r2 = tickets([bug("bug_r2", "make this button green", { interactions: [tap("transfer-submit")] })], fake([{ file: T, search: line, replace: line.replace("onClick={submit}", "onClick={submit} style={{ colour: 5 }}") }])).results[0];
    check(r2.state === "failed" && mtimes() === before, "(4) type error: failed, real src untouched", r2.steps.join("; ").slice(0, 160));
    const css = "src/styles.css";
    const first = readFileSync(join(app, css), "utf8").split("\n").find((l) => l.trim().endsWith("{"));
    const r3 = tickets([bug("bug_r3", "make this button green", { interactions: [tap("transfer-submit")] })], fake([{ file: css, search: first, replace: `${first}\n  color: red; {` }])).results[0];
    check(r3.state === "failed" && mtimes() === before, "(4) broken CSS: failed, real src untouched", r3.steps.join("; ").slice(0, 160));
  }

  // (1c) elements.json is client code: an edit to it is dropped, the rest of the change goes through
  {
    const kb = readFileSync(join(app, "src/pineo/elements.json"), "utf8");
    const line = submit();
    const kbLine = kb.split("\n").find((l) => l.includes('"id": "transfer-submit"'));
    const r = tickets([bug("bug_r4", "change this button text to Pay", { sessionId: "s1", interactions: [tap("transfer-submit")] })], fake([
      { file: T, search: line, replace: line.replace("onClick={submit}", 'onClick={submit} title="pay"') },
      { file: "src/pineo/elements.json", search: kbLine, replace: kbLine.replace('"label": "', '"label": "Pay ') },
    ], "Button now says Pay")).results[0];
    check(r.state === "failed" && readFileSync(join(app, "src/pineo/elements.json"), "utf8") === kb, "(1c) the fast path refuses an edit that names elements.json", r.steps.join("; ").slice(0, 200));
    const r2 = tickets([bug("bug_r5", "give this button a title", { sessionId: "s1", interactions: [tap("transfer-submit")] })], fake([{ file: T, search: line, replace: line.replace("onClick={submit}", 'onClick={submit} title="pay"') }], "Send button has a title now, which is a very long summary that goes on and on and on for the phone")).results[0];
    check(r2.state === "done" && r2.files.join() === T && submit().includes('title="pay"'), "a good edit is committed to the real src after the checks", JSON.stringify(r2.files));
    check(r2.summary.length <= 70, "summaries are cut to 70 characters for the phone", `${r2.summary.length}: ${r2.summary}`);
  }

  // A type error that was already in the app (someone mid-edit in vite.config.ts) must not block every change.
  {
    const cfgFile = join(app, "vite.config.ts");
    const cfg = readFileSync(cfgFile, "utf8");
    writeFileSync(cfgFile, `${cfg}\nconst alreadyBroken: number = "not a number";\n`);
    const line = submit();
    const ok = tickets([bug("bug_r5b", "give this button a tooltip", { sessionId: "s0", interactions: [tap("transfer-submit")] })], fake([{ file: T, search: line, replace: line.replace("onClick={submit}", 'onClick={submit} aria-label="send"') }], "Send button has a tooltip")).results[0];
    check(ok.state === "done", "an error that was already in the app does not block a good edit", ok.steps.join("; ").slice(0, 120));
    const line2 = submit();
    const bad = tickets([bug("bug_r5c", "make this button green", { sessionId: "s0", interactions: [tap("transfer-submit")] })], fake([{ file: T, search: line2, replace: line2.replace("<button", "<buton") }])).results[0];
    check(bad.state === "failed" && submit() === line2, "…but a new error from the edit itself is still caught", bad.steps.join("; ").slice(0, 120));
    writeFileSync(cfgFile, cfg);
    tickets([bug("bug_r5d", "undo", { sessionId: "s0" })]);
  }

  // (3)/(10) undo is scoped to the current hub session
  {
    const line = submit();
    const other = tickets([bug("bug_r6", "undo", { sessionId: "s2" }), bug("bug_r7", "put everything back", { sessionId: "s2" })]).results;
    check(other.every((r) => r.state === "failed") && submit() === line, "(3)(10) 'undo' and 'put everything back' in a NEW session leave the earlier session's change alone", other.map((r) => r.summary).join(" | "));
    const same = tickets([bug("bug_r8", "undo", { sessionId: "s1" })]).results[0];
    check(same.state === "done" && same.path === "undo" && !submit().includes('title="pay"'), "(3) 'undo' in the same session restores it", `${same.summary}`);
    writeFileSync(join(tmp, "state", "history-keepme.json"), "[]");
    const out = execFileSync("sh", [join(AGENT_DIR, "reset-app.sh"), "clear-history"], { encoding: "utf8", env: { ...process.env, PINEO_AGENT_STATE: join(tmp, "state"), PINEO_APP_DIR: app } });
    check(readdirSync(join(tmp, "state")).filter((f) => f.startsWith("history-")).length === 0, "(3) reset-app.sh clear-history removes the history files", out.trim());
  }

  // (5) MiniMax not answering: one short failed message, quickly, no session
  {
    const refused = tickets([bug("bug_r9", "make this button green", { interactions: [tap("transfer-submit")] })], { MINIMAX_ANTHROPIC_BASE_URL: "http://127.0.0.1:9" });
    const r = refused.results[0];
    check(r.state === "failed" && r.path === "outage" && r.ms < 3000 && /not answering/.test(r.summary), "(5) connection refused: failed in under 3 s with a human message, no retries, no session", `${r.ms} ms: ${r.summary}`);
    const hang = createServer(() => {}); // accepts and never answers
    await new Promise((r) => hang.listen(0, "127.0.0.1", r));
    const child = spawn(process.execPath, ["--input-type=module", "-e", `process.argv[1]="x"; const m = await import(${JSON.stringify(join(AGENT_DIR, "hub-runner.mjs"))}); const t0=Date.now(); const r = await m.fixTicket(${JSON.stringify(bug("bug_r10", "make this button green", { interactions: [tap("transfer-submit")] }))}); console.log("RESULTS "+JSON.stringify([{...r, ms: Date.now()-t0}]));`], { env: { ...process.env, PINEO_APP_DIR: app, PINEO_AGENT_STATE: join(tmp, "state"), MINIMAX_ANTHROPIC_BASE_URL: `http://127.0.0.1:${hang.address().port}` } });
    let out = "";
    child.stdout.on("data", (d) => (out += d));
    await new Promise((r) => child.on("close", r));
    hang.close();
    const h = JSON.parse(/^RESULTS (.*)$/m.exec(out)[1])[0];
    check(h.state === "failed" && h.path === "outage" && h.ms >= 14000 && h.ms < 18000, "(5) a model that accepts and never answers: failed after the 15 s limit, not minutes", `${h.ms} ms: ${h.summary}`);
  }
} catch (error) {
  check(false, "test crashed", String(error.stack ?? error).slice(0, 1500));
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
console.log(failed === 0 ? "\nREVIEW ITEMS OK" : `\nREVIEW ITEMS FAILED: ${failed} check(s)`);
process.exit(failed === 0 ? 0 : 1);
