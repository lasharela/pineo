#!/usr/bin/env node
// Pineo v2 coding agent: tickets from the hub -> live edits in mobile-app/src (Vite hot-reloads them).
//   node hub-runner.mjs [--app <mobile-app dir>] [--hub <ws url>] [--no-fast] [--no-fallback]
// One ticket at a time, with memory of the last ten (agent/.state), so "turn it back" works. Fast path: one direct MiniMax call returns search/replace edits for
// source the runner already located. Fallback: a headless `claude` session on MiniMax.
import { execFile } from "node:child_process";
import { cpSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { createHash } from "node:crypto";
import { AGENT_DIR, PINEO_ROOT, claudeArgs, claudeEnv, loadConfig, runClaude, sleep } from "./lib/common.mjs";

const argv = process.argv.slice(2);
const opt = (name) => {
  const i = argv.indexOf(name);
  return i === -1 ? undefined : argv[i + 1];
};
const APP = resolve(opt("--app") ?? process.env.PINEO_APP_DIR ?? join(PINEO_ROOT, "mobile-app"));
const SRC = join(APP, "src");
const HUB = opt("--hub") ?? process.env.PINEO_HUB_WS ?? "ws://127.0.0.1:8787/ws?role=agent";
const USE_FAST = !argv.includes("--no-fast");
const USE_FALLBACK = !argv.includes("--no-fallback") && !process.env.PINEO_NO_FALLBACK;
const KB = join(SRC, "pineo", "elements.json");

const log = (msg) => process.stdout.write(`[pineo-hub-agent ${new Date().toISOString().slice(11, 23)}] ${msg}\n`);
const cfg = loadConfig();
if (!cfg.apiKey) {
  process.stderr.write("pineo-hub-agent: MINIMAX_API_KEY is not set (pineo/.env)\n");
  process.exit(1);
}
if (!existsSync(SRC)) {
  process.stderr.write(`pineo-hub-agent: no src/ in ${APP}\n`);
  process.exit(1);
}

// --- source files -------------------------------------------------------------
// Nothing is written to the real src until it has passed every check. All edits (fast path,
// undo, full session) are made in WORK, a private copy of the app; `commit` then copies the
// changed files across, so a broken edit never reaches Vite or the phone.

const STATE_DIR = process.env.PINEO_AGENT_STATE ?? join(AGENT_DIR, ".state");
const APP_KEY = createHash("sha1").update(APP).digest("hex").slice(0, 10);
const WORK = join(STATE_DIR, `overlay-${APP_KEY}`);

const TEXT = /\.(tsx?|jsx?|css|json|html|svg|md)$/;
/** rel path (from the app root, "src/...") -> content, for every text file under <root>/src. */
export function readSrc(root = APP) {
  const out = new Map();
  (function walk(dir) {
    for (const d of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, d.name);
      if (d.isDirectory()) walk(p);
      else if (d.isFile() && TEXT.test(d.name)) out.set(relative(root, p).split(sep).join("/"), readFileSync(p, "utf8"));
    }
  })(join(root, "src"));
  return out;
}

/** Make <root>/src exactly `before` again. Returns the files that had differed. */
export function revert(before, root = WORK) {
  const now = readSrc(root);
  const touched = [];
  for (const [rel, text] of before) {
    if (now.get(rel) !== text) {
      mkdirSync(dirname(join(root, rel)), { recursive: true });
      writeFileSync(join(root, rel), text);
      touched.push(rel);
    }
  }
  for (const rel of now.keys()) {
    if (!before.has(rel)) {
      rmSync(join(root, rel), { force: true });
      touched.push(rel);
    }
  }
  return touched;
}

const changedSince = (before, root = WORK) => {
  const now = readSrc(root);
  const out = [];
  for (const [rel, text] of now) if (before.get(rel) !== text) out.push(rel);
  for (const rel of before.keys()) if (!now.has(rel)) out.push(rel);
  return out.sort();
};

/** Bring the private copy level with the real app as it is right now (`before` = its src). */
export function prepareWork(before) {
  mkdirSync(WORK, { recursive: true });
  const link = join(WORK, "node_modules");
  let linked = false;
  try {
    linked = lstatSync(link).isSymbolicLink();
  } catch {}
  if (!linked && existsSync(join(APP, "node_modules"))) symlinkSync(join(APP, "node_modules"), link);
  for (const d of readdirSync(APP, { withFileTypes: true })) {
    if (d.isFile() && !d.name.startsWith(".env")) cpSync(join(APP, d.name), join(WORK, d.name));
  }
  if (existsSync(join(APP, "scripts"))) cpSync(join(APP, "scripts"), join(WORK, "scripts"), { recursive: true });
  if (!existsSync(join(WORK, "src"))) cpSync(SRC, join(WORK, "src"), { recursive: true }); // images and the like, once
  revert(before, WORK);
}

/** Copy what changed in the private copy into the real src. Returns [{file, before, after}]. */
function commit(before) {
  const now = readSrc(WORK);
  const changes = changedSince(before, WORK).map((file) => ({ file, before: before.get(file) ?? null, after: now.get(file) ?? null }));
  for (const c of changes) {
    if (c.after === null) rmSync(join(APP, c.file), { force: true });
    else {
      mkdirSync(dirname(join(APP, c.file)), { recursive: true });
      writeFileSync(join(APP, c.file), c.after);
    }
  }
  return changes;
}

/** Everything in src/pineo/ is the in-page client, elements.json included: changing it reloads the whole page on the phone. */
const isClientCode = (rel) => rel.startsWith("src/pineo/");
const pineoIds = (files) => {
  const ids = [];
  for (const [rel, text] of files) {
    if (rel.startsWith("src/pineo/")) continue;
    for (const m of text.matchAll(/(?:data-pineo-id\s*=\s*|pineoId\s*[=:]\s*|\bpineo\()\{?\s*["']([^"']+)["']/g)) ids.push(m[1]);
  }
  return ids.sort().join(",");
};

const run = (cmd, args, cwd, timeout = 30000) =>
  new Promise((done) => {
    execFile(cmd, args, { cwd, timeout, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
      done({ ok: !error, out: `${stdout}${stderr}`.trim() });
    });
  });

const tsErrors = (out) => new Set(out.split("\n").filter((l) => /error TS\d+/.test(l)).map((l) => l.replace(/\(\d+,\d+\)/, "")));
/**
 * TypeScript errors this edit introduced ("" if none). If tsc fails, it is run once more on the
 * state before the edit: an error that was already there (someone half-way through editing
 * vite.config.ts, say) is not the edit's fault and must not block every change.
 */
async function typeErrors(root, tsc, before, now) {
  const after = await run(tsc, ["--noEmit"], root);
  if (after.ok) return "";
  const mine = tsErrors(after.out);
  revert(before, root);
  const base = await run(tsc, ["--noEmit"], root);
  revert(now, root); // put the edit back for the caller
  const old = base.ok ? new Set() : tsErrors(base.out);
  const fresh = [...mine].filter((l) => !old.has(l));
  if (fresh.length === 0) {
    log(`  note: TypeScript already reported ${old.size} error(s) before this edit (${[...old][0]?.slice(0, 120)}); ignored`);
    return "";
  }
  return `TypeScript: ${fresh[0].slice(0, 200)}`;
}

/** Does the app still compile, and is everything the contract protects intact? "" when fine. */
export async function verify(before, root = WORK) {
  const changed = changedSince(before, root);
  if (changed.length === 0) return "nothing was changed";
  const bad = changed.find((rel) => isClientCode(rel));
  if (bad) return `edited Pineo client code (${bad})`;
  const now = readSrc(root);
  if (pineoIds(now) !== pineoIds(before)) return "a data-pineo-id was added, removed or renamed";
  for (const rel of changed) {
    const text = now.get(rel);
    if (text === undefined) return `deleted ${rel}`;
    if (rel.endsWith(".css")) {
      const bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/"[^"\n]*"|'[^'\n]*'/g, "");
      if ((bare.match(/\{/g) ?? []).length !== (bare.match(/\}/g) ?? []).length) return `unbalanced braces in ${rel}`;
    }
    if (rel.endsWith(".json")) {
      try {
        JSON.parse(text);
      } catch {
        return `${rel} is no longer valid JSON`;
      }
    }
  }
  const tsc = join(root, "node_modules/.bin/tsc");
  const checks = [];
  if (existsSync(tsc)) checks.push(typeErrors(root, tsc, before, now));
  if (existsSync(join(root, "scripts/kb-check.mjs"))) {
    checks.push(run(process.execPath, ["scripts/kb-check.mjs"], root).then((r) => (r.ok ? "" : `kb-check: ${r.out.split("\n").slice(0, 2).join(" ").slice(0, 200)}`)));
  }
  return (await Promise.all(checks)).find((e) => e) ?? "";
}

// --- memory: what was changed before, so follow-ups and undo work ---------------

// One history per app folder, so tests on a scratch copy never mix with the real app's.
const HISTORY_FILE = join(STATE_DIR, `history-${APP_KEY}.json`);
const HISTORY_KEEP = 10;
const HISTORY_IN_PROMPT = 5;
const HISTORY_MAX_AGE_MS = 3 * 60 * 60 * 1000;

/** [{ seq, bugId, at, said, ids, summary, files, kind: "change"|"undo", undone, undid?, changes: [{file, before, after}] }] oldest first. */
export function loadHistory() {
  try {
    const list = JSON.parse(readFileSync(HISTORY_FILE, "utf8"));
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}
function saveHistory(list) {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(HISTORY_FILE, JSON.stringify(list.slice(-HISTORY_KEEP)));
}

/** Record what a finished ticket changed: exactly the files the runner itself wrote, before and after. */
function remember(history, bug, ctx, changes, summary, kind, undid = []) {
  for (const e of history) if (undid.includes(e.seq)) e.undone = true;
  history.push({ seq: (history.at(-1)?.seq ?? 0) + 1, bugId: bug.id, session: bug.sessionId ?? null, at: Date.now(), said: ctx.transcript, ids: ctx.ids, summary, files: changes.map((c) => c.file), kind, undone: false, ...(undid.length ? { undid } : {}), changes });
  history.splice(0, Math.max(0, history.length - HISTORY_KEEP));
  saveHistory(history);
}

/** The changed lines of one file as "- old / + new", without the unchanged head and tail. */
export function compactDiff(before, after, max = 24) {
  const a = (before ?? "").split("\n");
  const b = (after ?? "").split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  const cut = (lines, sign) => {
    const out = lines.slice(0, max).map((l) => `  ${sign} ${l}`);
    if (lines.length > max) out.push(`  ${sign} … (${lines.length - max} more lines)`);
    return out;
  };
  return [`  @ line ${head + 1}`, ...cut(a.slice(head, a.length - tail), "-"), ...cut(b.slice(head, b.length - tail), "+")].join("\n");
}

const recent = (history) => history.filter((e) => Date.now() - e.at < HISTORY_MAX_AGE_MS).slice(-HISTORY_IN_PROMPT);

function historyText(history, session = null) {
  const list = recent(history);
  if (list.length === 0) return "";
  let text = `\nEARLIER CHANGES YOU MADE IN THIS SESSION (oldest first). Words like "it", "that", "back", "again", "darker", "the other one", "same for…" refer to these, most likely the last one that is not undone:\n`;
  for (const e of list) {
    text += `[#${e.seq}]${(e.session ?? null) !== session ? " (FROM AN EARLIER SESSION: leave it alone unless they clearly mean it)" : ""}${e.undone ? " (ALREADY UNDONE)" : ""}${e.kind === "undo" ? ` (this was an undo of ${e.undid.map((n) => `#${n}`).join(", ")})` : ""} they said ${JSON.stringify(e.said)}; selected: ${e.ids.join(", ") || "nothing"}; result: ${e.summary}\n`;
    for (const c of e.changes) text += ` ${c.file}:\n${compactDiff(c.before, c.after)}\n`;
  }
  return text;
}

const NUMBER_WORDS = { one: 1, two: 2, three: 3, four: 4, five: 5, couple: 2, both: 2 };
/**
 * Is this, word for word, nothing but "undo the last change(s)"? Returns how many (0 = no).
 * Anything with more in it ("turn the colours back", "undo the green") is left to the model,
 * which sees the history and can name the change to undo.
 */
export function undoCount(text) {
  let t = text.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  t = t.replace(/^((please|ok|okay|no|now|just|actually|and|so|hey|can you|could you|would you) )+/, "").replace(/( (please|now|again|for me))+$/, "");
  const m = /^(undo|revert|roll ?back|go back|(?:turn|put|change|take|bring|switch|set) (?:it|that|this|them|everything|all) back)(?: (it|that|this|them|everything|all))?(?: (?:the |my )?(?:last|previous)(?: (\d|one|two|three|four|five|couple))?(?: (?:changes?|ones?|things?|edits?))?)?$/.exec(t);
  if (!m) return 0;
  if (/everything|all/.test(`${m[1]} ${m[2] ?? ""}`)) return HISTORY_KEEP;
  return m[3] ? (NUMBER_WORDS[m[3]] ?? Number(m[3])) : 1;
}

/**
 * Put files back from the saved before-content of these history entries (newest first), with no
 * model involved. All or nothing: if any file is not exactly as that change left it, nothing is written.
 */
function restoreFrom(entries, before) {
  const work = new Map(); // file -> content|null, as it will be
  const cur = (file) => (work.has(file) ? work.get(file) : (before.get(file) ?? null));
  for (const e of entries) {
    if (e.changes.length === 0) return { ok: false, why: `change #${e.seq} has nothing recorded to restore` };
    for (const c of e.changes) {
      if (cur(c.file) === c.after) {
        work.set(c.file, c.before);
        continue;
      }
      // The file has moved on (someone else edited it too). Take back just this change's lines,
      // if they are still there exactly once; otherwise leave it to the model.
      const back = reverseHunk(cur(c.file), c.before, c.after);
      if (back === undefined) return { ok: false, why: `${c.file} has been changed since change #${e.seq}` };
      work.set(c.file, back);
    }
  }
  for (const [file, text] of work) {
    if (text === null) rmSync(join(WORK, file), { force: true });
    else writeFileSync(join(WORK, file), text);
  }
  return { ok: true };
}

/** `now` is neither before nor after: swap the lines that before->after changed back, if they occur exactly once. */
export function reverseHunk(now, before, after) {
  if (typeof now !== "string" || typeof before !== "string" || typeof after !== "string") return undefined;
  const a = before.split("\n");
  const b = after.split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  // One line of context on each side makes a pure insertion or deletion findable too.
  const from = Math.max(0, head - 1);
  const newBlock = b.slice(from, Math.min(b.length, b.length - tail + 1)).join("\n");
  const oldBlock = a.slice(from, Math.min(a.length, a.length - tail + 1)).join("\n");
  if (newBlock === "" || newBlock === oldBlock) return undefined;
  const at = now.indexOf(newBlock);
  if (at === -1 || now.indexOf(newBlock, at + 1) !== -1) return undefined;
  return now.slice(0, at) + oldBlock + now.slice(at + newBlock.length);
}

/**
 * The last `n` changes still in effect, newest first, and only those made in this hub session:
 * "undo" and "put everything back" at the demo must never reach into a rehearsal.
 */
const undoable = (history, n, session = null) => history.filter((e) => e.kind === "change" && !e.undone && (e.session ?? null) === session).slice(-n).reverse();

// --- the ticket -> what to show the model -------------------------------------

/**
 * Element ids the person meant: the ones still selected at submit, most recently selected first.
 * A tap with selected:false takes the element out again; a circle selects everything inside it.
 * Nothing else counts. contextAtOpen.lastTappedElementId and the ids the hub seeds from it are
 * whatever was tapped BEFORE the ticket (a tab, the previous ticket's element), not a selection.
 */
export function targetIds(bug) {
  const selected = []; // oldest first
  const drop = (id) => {
    const at = selected.indexOf(id);
    if (at !== -1) selected.splice(at, 1);
  };
  const add = (id) => {
    if (typeof id !== "string" || !id) return;
    drop(id);
    selected.push(id);
  };
  for (const it of bug.interactions ?? []) {
    if (it.kind === "circle") {
      for (const id of it.elementIds ?? []) add(id);
      if (it.elementId) add(it.elementId);
    } else if (it.kind === "tap" && it.elementId) {
      if (it.selected === false) drop(it.elementId);
      else add(it.elementId);
    }
  }
  return selected.reverse();
}

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function locate(id, files) {
  const re = new RegExp(`["']${esc(id)}["']`);
  const hits = [];
  for (const [rel, text] of files) {
    if (rel.startsWith("src/pineo/")) continue;
    const lines = text.split("\n");
    lines.forEach((l, i) => {
      if (re.test(l)) hits.push({ file: rel, line: i + 1 });
    });
  }
  return hits;
}

const MAX_FILE_LINES = 450;
const MAX_PROMPT_LINES = 1100;
const STOP = new Set("a an the this that these those it its is are be to of on in at for with and or but make makes change changes turn set put look looks like please can you could would i we want need should so just all every each some more less bit little very too them they their my our now here there one ones thing things".split(" "));
const words = (text) => String(text ?? "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !STOP.has(w));
/** "switchers" ~ "switch", "buttons" ~ "button", "colour" ~ "colours". */
const alike = (a, b) => a === b || (a.length >= 4 && b.length >= 4 && (a.startsWith(b) || b.startsWith(a))) || (a.length >= 6 && b.length >= 6 && a.slice(0, 5) === b.slice(0, 5));

/** Nothing selected: which elements on the current screen do the spoken words fit? Best first. */
export function candidates(transcript, screen, visible, kb) {
  const said = words(transcript);
  const vis = new Set(visible ?? []);
  const scored = [];
  for (const e of kb) {
    const here = vis.has(e.id) || (screen && (e.screen === screen || (e.screen === "*" && vis.has(e.id))));
    if (!here) continue;
    const strong = [...words(e.label), ...words((e.keywords ?? []).join(" ")), ...words(String(e.id).replace(/-/g, " "))];
    const weak = words(e.description);
    let score = 0;
    for (const w of said) score += strong.some((x) => alike(w, x)) ? 2 : weak.some((x) => alike(w, x)) ? 1 : 0;
    if (score > 0) scored.push({ id: e.id, score: score + (vis.has(e.id) ? 0.5 : 0), entry: e });
  }
  return scored.sort((x, y) => y.score - x.score).slice(0, 8);
}

/** "l a s h a, Lasha" -> "Lasha"; "l a s h a" -> "Lasha". Speech-to-text writes spelled-out words as single letters. */
export function normalizeSpelled(text) {
  return String(text).replace(/\b((?:[A-Za-z][\s,.-]+){2,}[A-Za-z])\b([\s,.:;-]*)([A-Za-z]+)?/g, (all, letters, gap, next) => {
    const word = letters.replace(/[^A-Za-z]/g, "");
    if (next && next.toLowerCase() === word.toLowerCase()) return next;
    const proper = word[0].toUpperCase() + word.slice(1).toLowerCase();
    return `${proper}${gap}${next ?? ""}`;
  });
}

const CONTENT = /\b(name|named|called|greeting|text|says?|title|label|word|amount|balance|price|payment|fee|total|date|euros?|dollars?|percent)\b|[€$£%]|\d/i;
const SYNONYMS = { greeting: ["good morning", "good afternoon", "good evening", "hello", "welcome"] };
const PERSON = /\b(my name|name is|call me|i am called|i'm called|user ?name)\b/i;
/** JSX attributes whose value the person can read on screen; every other attribute value is ignored. */
const SHOWN_ATTR = new Set(["title", "sub", "label", "placeholder", "aria-label", "alt", "value", "text", "name", "first", "last"]);

/**
 * Nothing selected and they talk about what the app SAYS (a name, an amount, a label): find it in
 * the source by its text. Returns { tokens, hits: [{file, line, text, score}], content }.
 */
export function textHits(transcript, files) {
  const said = normalizeSpelled(transcript);
  const tokens = new Set(); // distinctive things they said: quoted words, numbers, names, the word after "not"/"from"
  for (const m of said.matchAll(/["“]([^"”]{2,40})["”]/g)) tokens.add(m[1].toLowerCase());
  for (const m of said.matchAll(/\d[\d.,]*\d|\d/g)) tokens.add(m[0].replace(/[.,]$/, ""));
  const sentenceStart = new Set([...said.matchAll(/(?:^|[.!?]\s+)([A-Z][a-z]+)/g)].map((m) => m[1]));
  for (const m of said.matchAll(/\b[A-Z][a-z]{2,}\b/g)) if (!sentenceStart.has(m[0])) tokens.add(m[0].toLowerCase());
  for (const m of said.matchAll(/\b(?:not|instead of|from|called|named|says?|say|reads?)\s+([A-Za-z][\w'-]{2,})/gi)) if (!STOP.has(m[1].toLowerCase())) tokens.add(m[1].toLowerCase());
  const plain = words(said);
  const extra = plain.flatMap((w) => Object.entries(SYNONYMS).filter(([k]) => alike(w, k)).flatMap(([, v]) => v));
  const person = PERSON.test(said);
  const amount = /\d/.test(said);
  const content = CONTENT.test(said) || tokens.size > 0 || person;
  const hits = [];
  for (const [rel, body] of files) {
    if (isClientCode(rel) || !/\.(tsx?|jsx?)$/.test(rel)) continue;
    const data = /(^|\/)data\.[jt]sx?$/.test(rel);
    let inUser = 0; // lines left inside a `USER = {` / `profile: {` block
    body.split("\n").forEach((raw, i) => {
      if (/\b(user|profile|me|account ?holder|customer)\b\s*[:=]\s*\{/i.test(raw)) inUser = 8;
      else if (inUser > 0) inUser--;
      // What the person can read: string literals and JSX text, minus attribute values nobody sees.
      const l = raw.replace(/\b([\w-]+)=(?:"[^"]*"|'[^']*')/g, (all, name) => (SHOWN_ATTR.has(name) ? all : ""));
      const shown = [...l.matchAll(/"([^"\n]{1,120})"|'([^'\n]{1,120})'|`([^`\n]{1,120})`|>([^<>{}\n]{2,120})(?=<|\{|$)/g)].map((m) => m[1] ?? m[2] ?? m[3] ?? m[4]).join(" | ").toLowerCase();
      let score = 0;
      if (person) {
        if (/\b(first|last|full|display)?_?name\s*:|\b(first|last)\s*:/i.test(raw) && inUser > 0) score += 6;
        if (/\{\s*(USER|user|profile|me)\.\w+/.test(raw)) score += 3;
      }
      if (shown) {
        for (const t of tokens) if (shown.includes(t)) score += 4;
        for (const t of extra) if (shown.includes(t)) score += 3;
        const shownWords = words(shown);
        let wordHits = 0;
        for (const w of plain) if (w.length >= 4 && shownWords.some((x) => alike(w, x))) wordHits++;
        score += wordHits * (data ? 2 : 1);
        if (wordHits && amount && /-?\d+\.\d\d?\b|\d{3,}/.test(raw)) score += 2; // a row with an amount, and they named an amount
      }
      if (score >= 2) hits.push({ file: rel, line: i + 1, text: raw.trim().slice(0, 160), score: score + (data ? 0.5 : 0) });
    });
  }
  hits.sort((x, y) => y.score - x.score);
  return { tokens: [...tokens], hits: hits.slice(0, 14), content, said };
}

function buildContext(bug, files, history = [], extraFiles = []) {
  const ids = targetIds(bug).slice(0, 6);
  let kb = [];
  try {
    kb = JSON.parse(files.get("src/pineo/elements.json") ?? "[]");
  } catch {}
  const screen = bug.screen || bug.contextAtOpen?.screen || "";
  const heard = (bug.transcript ?? []).map((t) => (typeof t === "string" ? t : t?.text)).filter(Boolean).join(" ").trim();
  const transcript = normalizeSpelled(heard);
  const shown = new Map(); // rel -> text, most relevant first
  let total = 0;
  const show = (rel, limit = 99) => {
    const body = files.get(rel);
    if (body === undefined || shown.has(rel) || shown.size >= limit || isClientCode(rel)) return false;
    const n = body.split("\n").length;
    if (n > MAX_FILE_LINES || (shown.size > 0 && total + n > MAX_PROMPT_LINES)) return false;
    shown.set(rel, body);
    total += n;
    return true;
  };
  const screenFile = [...files.keys()].find((rel) => screen && /\.(tsx|jsx)$/.test(rel) && rel.toLowerCase().endsWith(`/${String(screen).toLowerCase()}.${rel.split(".").pop()}`));

  for (const rel of extraFiles) show(rel);
  const where = [];
  for (const id of ids) {
    const entry = kb.find((e) => e.id === id);
    const hits = locate(id, files);
    where.push({ id, label: entry?.label, description: entry?.description, at: hits.map((h) => `${h.file}:${h.line}`) });
    for (const h of hits.slice(0, 2)) show(h.file, 4);
  }
  // Nothing selected: the words against the elements of the screen they are on, then that screen's file,
  // then whatever the previous change touched (a follow-up like "make it darker" is about that).
  let maybe = [];
  let found = { tokens: [], hits: [], content: false };
  if (ids.length === 0) {
    // What the app says (a name, an amount, a label) is found by its text, wherever it lives:
    // usually the mock data file, which no screen-based guess would offer.
    found = textHits(transcript, files);
    if (found.content) {
      for (const h of found.hits.slice(0, 6)) show(h.file, 3);
      for (const rel of files.keys()) if (/^src\/data\.[jt]sx?$/.test(rel)) show(rel);
    }
    maybe = candidates(transcript, screen, bug.contextAtOpen?.visibleElementIds, kb).map((c) => ({ ...c, at: locate(c.id, files).map((h) => `${h.file}:${h.line}`) }));
    if (screenFile) show(screenFile);
    for (const c of maybe.slice(0, 5)) for (const at of c.at.slice(0, 1)) show(at.split(":")[0], 4);
    const last = recent(history).filter((e) => !e.undone).at(-1);
    for (const rel of last?.files ?? []) show(rel, 6);
  }
  // What the shown files are built from: their stylesheets, the app-wide stylesheet, and the small
  // local modules they import (shared components such as the switch live there).
  const importsOf = (rel, re) => {
    const out = [];
    for (const m of (files.get(rel) ?? "").matchAll(re)) {
      const base = join(dirname(rel), m[1]).split(sep).join("/");
      const target = [base, `${base}.tsx`, `${base}.ts`, `${base}.jsx`, `${base}.js`, `${base}/index.tsx`, `${base}/index.ts`].find((c) => files.has(c));
      if (target && !target.startsWith("src/pineo/")) out.push(target);
    }
    return out;
  };
  const CSS = /import\s+["']([^"']+\.css)["']/g;
  const MODULE = /from\s+["'](\.{1,2}\/[^"']+)["']/g;
  const main = [...shown.keys()];
  for (const rel of [...new Set([...main.flatMap((r) => importsOf(r, CSS)), ...["src/main.tsx", "src/App.tsx", "src/main.jsx", "src/App.jsx"].flatMap((r) => importsOf(r, CSS))])].slice(0, 4)) show(rel);
  const modules = [...new Set(main.flatMap((r) => importsOf(r, MODULE)))].filter((r) => /\.(tsx|jsx)$/.test(r) && (files.get(r) ?? "").split("\n").length <= 260);
  for (const rel of modules.slice(0, 2)) show(rel);

  const interactions = (bug.interactions ?? [])
    .filter((it) => it.kind === "tap" || it.kind === "circle")
    .map((it) => (it.kind === "circle" ? `circled ${(it.elementIds ?? []).join(", ") || "(nothing)"}` : `${it.selected === false ? "deselected" : "selected"} ${it.elementId ?? "(no element)"}${it.label ? ` ("${it.label}")` : ""}`));
  let text = `SPOKEN REQUEST: ${JSON.stringify(transcript)}\n`;
  text += `SCREEN THEY ARE ON: ${screen || "unknown"}${screenFile ? ` (${screenFile})` : ""}\n`;
  text += `WHAT THEY DID WHILE SPEAKING (in order): ${interactions.join("; ") || "nothing recorded"}\n`;
  if (where.length) {
    text += where.length === 1 ? `THE ELEMENT THEY SELECTED:\n` : `THE ${where.length} ELEMENTS THEY SELECTED (all still selected when they finished; most recent first). "These", "both", "them" mean all of them; "this" alone means the first:\n`;
    for (const w of where) text += `- ${w.id}: ${w.label ?? "?"} — ${w.description ?? ""} — in source at ${w.at.join(", ") || "not found"}\n`;
  } else {
    text += `THEY SELECTED NO ELEMENT. So the request is about the screen they are on, about something they name in words, or a follow-up to an earlier change.\n`;
    if (maybe.length) {
      text += `Elements on this screen that fit their words (best match first):\n`;
      for (const c of maybe.slice(0, 6)) text += `- ${c.id}: ${c.entry.label} — ${c.entry.description ?? ""} — in source at ${c.at.join(", ") || "not found"}\n`;
    }
    if (found.hits.length) {
      text += `Text in the source that fits their words${found.tokens.length ? ` (looked for: ${found.tokens.join(", ")})` : ""}; if the request is about what the app SAYS, the thing to change is most likely one of these:\n`;
      for (const h of found.hits.slice(0, 12)) text += `- ${h.file}:${h.line}: ${h.text}\n`;
    }
    const visible = (bug.contextAtOpen?.visibleElementIds ?? []).slice(0, 40);
    if (visible.length) text += `Element ids visible when they started: ${visible.join(", ")}\n`;
    const last = bug.contextAtOpen?.lastTappedElementId;
    if (last) text += `Weak hint only: the last thing tapped BEFORE this ticket was ${last} (often just navigation or the previous request; ignore it unless the words fit).\n`;
  }
  text += historyText(history, bug.sessionId ?? null);
  for (const [rel, body] of shown) text += `\n===== FILE ${rel} =====\n${body}\n===== END ${rel} =====\n`;
  const others = [...files.keys()].filter((rel) => !shown.has(rel) && !rel.startsWith("src/pineo/") && /\.(tsx?|jsx?|css)$/.test(rel));
  if (others.length) text += `\nOTHER SOURCE FILES (not shown; ask with "need" only if the code to change is certainly there): ${others.join(", ")}\n`;
  return { text, transcript, ids, maybe: maybe.map((c) => c.id), textHits: found.hits.slice(0, 5).map((h) => `${h.file}:${h.line}`), files: [...shown.keys()] };
}

const RULES = `Rules:
- Make the one change they asked for, as small as possible. Nothing else: no refactor, no reformatting, no other fixes.
- Change only what they mean: the element(s) they selected, or, when nothing is selected, what their words name on the current screen (all of that kind on this screen if they use a plural such as "the switches"), or what the earlier change they refer to touched. When several are selected and the words cover them ("these two buttons", "both", "make them match"), change every one of them, with one edit per place, in whichever files they are. If an element's CSS class is shared with other elements, do not edit the shared rule: give that one element an inline style prop (style={{ ... }}) or its own class.
- If they name no exact value ("change the color of this button"), choose one that is clearly different from the current one and looks good in this app; say which in the summary.
- Never remove, rename or add a data-pineo-id / pineoId. Never edit anything in src/pineo/ (that includes elements.json, even when a label text changes).
- The app must still compile (TypeScript + React). Keep JSX valid.
- FIRST decide whether they asked for a change. An explicit instruction counts ("change…", "make…", "rename…", "hide…", "this should say…"), and so does a correction of what the app shows ("my name is Lasha, not Maya", "the rent should be 1,450", "it's Tuesday, not Monday"): that means "show the right value". A question ("why…", "how…", "where…"), a remark, a vague complaint ("this looks wrong") or a bug report with no stated fix is NOT a change request: do not guess a fix, do not touch data or logic, refuse with a short reason. Also refuse if you cannot tell which element they mean, or if it already is the way they asked.
- Follow-ups: "darker", "a bit bigger", "same for the other buttons", "no, the other one" build on the earlier changes listed in the request; start from what those changed, in the files as they are now.
- Speech arrives in pieces and may stop mid-sentence. If the clear part is enough, act on it: "My name is Lasha. Not" means set the displayed user name to Lasha wherever the current name is shown (data file, greeting, profile), even though the old name was never said. Change displayed content where it is defined (usually the data file), so every place that shows it follows. Refuse only when there is truly nothing actionable.
- A refusal reason is at most ten plain, friendly words for the presenter's phone, e.g. "I didn't catch what to change about the balance."
- The summary is shown on the phone after "Done: ". At most 60 characters, plain words, no file names, no code, no colour codes, e.g. "Send button is now green".`;

const FAST_SYSTEM = `You change the source of a small React + Vite + TypeScript mobile banking app while it is being demoed live. The presenter said one change aloud and pointed at an element on the phone. You get their words, the element, and the source files that matter.

Answer with ONE JSON object and nothing else (no prose, no code fence):
{"ok":true,"summary":"...","edits":[{"file":"src/...","search":"...","replace":"..."}]}
or, when they want one or more of the EARLIER CHANGES taken back exactly as they were made ("turn the colours back", "undo the green", "revert that"): name them by number, newest first, and the runner restores the files itself (do not write edits for that):
{"ok":true,"undo":[4],"summary":"Reverted: switches are back to the original colour"}
or, only if the code that must change is in a file you were not shown:
{"need":["src/..."]}
or
{"ok":false,"reason":"<short reason for the presenter>"}

One answer may hold several edits, in several files. "search" must be text copied exactly from that file (same spaces and line breaks) and must occur exactly once in it: copy the whole line, or the line with its data-pineo-id, to be unique. "replace" is what goes there instead.

${RULES}`;

const FALLBACK_SYSTEM = `You change the source of a small React + Vite + TypeScript mobile banking app while it is being demoed live. The presenter said one change aloud and pointed at an element on the phone. Your working directory is the app's src/ folder; the paths you are given start at its parent. Use Read and Edit; be fast, the audience is waiting.

Before you use any tool, decide whether the words are an explicit instruction to change something. If they are a question, a remark or a complaint with no stated change ("this looks wrong"), use no tools and answer at once with RESULT ok:false and the reason.

${RULES}

The request may list EARLIER CHANGES with their exact before/after lines: use them for follow-ups and for undoing (put the old lines back with Edit).

Finish with exactly one line and nothing after it:
RESULT: {"ok":true,"summary":"..."}   or   RESULT: {"ok":false,"reason":"..."}`;

// --- the two ways to make the edit ---------------------------------------------

function parseJsonObject(text) {
  const open = text.indexOf("{");
  const close = text.lastIndexOf("}");
  if (open === -1 || close <= open) return undefined;
  try {
    return JSON.parse(text.slice(open, close + 1));
  } catch {
    return undefined;
  }
}

async function callOnce(system, user, signal) {
  const res = await fetch(`${cfg.baseUrl}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${cfg.apiKey}`, "anthropic-version": "2023-06-01" },
    body: JSON.stringify({ model: cfg.model, max_tokens: 8000, system, output_config: { effort: cfg.effort }, messages: [{ role: "user", content: user }] }),
    signal,
  });
  if (!res.ok) throw new Error(`MiniMax HTTP ${res.status}`);
  const json = await res.json();
  const text = (json.content ?? []).filter((b) => b.type === "text").map((b) => b.text).join("");
  if (!text.trim()) throw new Error("MiniMax answered with no text");
  return text;
}

const HEDGE_MS = (process.env.PINEO_HEDGE_MS ?? "3000,6000").split(",").map(Number).filter((n) => n > 0);
const FAST_TIMEOUT_MS = Number(process.env.PINEO_FAST_TIMEOUT_MS || 15000);
const SESSION_TIMEOUT_MS = Number(process.env.PINEO_AGENT_TIMEOUT_MS || 45000);
const NOT_ANSWERING = "The model is not answering, try again in a moment.";

/**
 * The same request normally takes 2 s but now and then 10-30 s. So: if the first call has not
 * answered after 3 s a second identical one is sent, after 6 s a third; the first answer
 * wins and the others are aborted. Rejects only when every call failed or the limit passed.
 */
async function callMiniMax(system, user) {
  const all = new AbortController();
  const timers = [];
  let live = 0;
  let sent = 0;
  let lastError = new Error("MiniMax did not answer in time");
  try {
    return await new Promise((resolveFirst, reject) => {
      const launch = () => {
        live++;
        const n = ++sent;
        const t0 = Date.now();
        callOnce(system, user, all.signal).then(
          (text) => {
            if (n > 1) log(`  hedge: call ${n} of ${sent} answered first (${Date.now() - t0} ms after it was sent)`);
            resolveFirst(text);
          },
          (error) => {
            lastError = error;
            if (--live === 0 && sent > HEDGE_MS.length) reject(lastError);
            else if (live === 0) launch(); // failed fast: do not wait for the hedge timer
          },
        );
      };
      launch();
      for (const ms of HEDGE_MS) timers.push(setTimeout(() => sent <= HEDGE_MS.length && launch(), ms));
      timers.push(setTimeout(() => reject(lastError), FAST_TIMEOUT_MS));
    });
  } finally {
    for (const t of timers) clearTimeout(t);
    all.abort();
  }
}

/** Replace `search` once in `text`; tolerate differing indentation. undefined when it does not match exactly once. */
export function applyEdit(text, search, replace) {
  if (typeof search !== "string" || typeof replace !== "string" || search === "") return undefined;
  const first = text.indexOf(search);
  if (first !== -1) return text.indexOf(search, first + 1) === -1 ? text.slice(0, first) + replace + text.slice(first + search.length) : undefined;
  // Same lines, different leading/trailing whitespace.
  const want = search.split("\n").map((l) => l.trim());
  while (want.length && want[0] === "") want.shift();
  while (want.length && want[want.length - 1] === "") want.pop();
  if (want.length === 0) return undefined;
  const lines = text.split("\n");
  const at = [];
  for (let i = 0; i + want.length <= lines.length; i++) if (want.every((w, k) => lines[i + k].trim() === w)) at.push(i);
  if (at.length !== 1) return undefined;
  const indent = /^\s*/.exec(lines[at[0]])[0];
  const rep = replace.split("\n");
  const repIndent = /^\s*/.exec(rep.find((l) => l.trim() !== "") ?? "")[0];
  const fitted = rep.map((l) => (l.trim() === "" ? l : indent + (l.startsWith(repIndent) ? l.slice(repIndent.length) : l.trimStart())));
  return [...lines.slice(0, at[0]), ...fitted, ...lines.slice(at[0] + want.length)].join("\n");
}

/** Thrown inside fixTicket when a fuller copy of the ticket arrived: stop, put everything back, start again. */
class Amended extends Error {}
const ABORT = Symbol("amended");
/** A signal that never fires, for callers that do not amend. */
const NEVER = { aborted: false, promise: new Promise(() => {}) };
export function makeSignal() {
  let fire;
  const s = { aborted: false, bug: null };
  s.promise = new Promise((r) => (fire = r));
  s.abort = (bug) => {
    s.aborted = true;
    s.bug = bug;
    fire(ABORT);
  };
  return s;
}

/** One model call. Returns { refused } | { retry, why } | { need } | { undo, summary } | { summary } (edits written). */
async function fastPath(ctx, before, feedback = "", signal = NEVER) {
  const t0 = Date.now();
  let answer;
  try {
    const fake = process.env.PINEO_FAKE_FAST_ANSWER; // tests only: stands in for the model's answer
    answer = parseJsonObject(fake ?? await callMiniMax(FAST_SYSTEM, feedback ? `${ctx.text}\n\nNOTE FROM THE RUNNER: ${feedback}\n` : ctx.text));
  } catch (error) {
    return { retry: true, why: error.message, modelMs: Date.now() - t0, transport: true };
  }
  const modelMs = Date.now() - t0;
  if (signal.aborted) return { retry: true, why: "superseded by a fuller transcript", modelMs }; // never write for a stale request
  if (!answer) return { retry: true, why: "the answer was not one JSON object", modelMs };
  if (Array.isArray(answer.need) && answer.need.length) return { need: answer.need.map((f) => String(f).replace(/^\.?\//, "")).slice(0, 3), modelMs };
  if (answer.ok === false) return { refused: String(answer.reason ?? "not an actionable change").slice(0, 200), modelMs };
  if (Array.isArray(answer.undo) && answer.undo.length) return { undo: answer.undo.map(Number).filter(Number.isFinite), summary: String(answer.summary ?? "").slice(0, 200), modelMs };
  if (!Array.isArray(answer.edits)) return { retry: true, why: "the answer had no edits", modelMs };
  if (answer.edits.length === 0) return { refused: String(answer.summary || "Nothing to change: it is already like that.").slice(0, 200), modelMs };
  const next = new Map();
  for (const e of answer.edits) {
    const rel = String(e.file ?? "").replace(/^\.?\//, "");
    if (!rel.startsWith("src/") || rel.includes("..") || isClientCode(rel) || !before.has(rel)) return { retry: true, why: `the edit names a file that does not exist or may not be edited: ${rel}`, modelMs };
    const out = applyEdit(next.get(rel) ?? before.get(rel), e.search, e.replace);
    if (out === undefined) {
      const n = typeof e.search === "string" && e.search ? before.get(rel).split(e.search).length - 1 : 0;
      return { retry: true, why: `in ${rel} the "search" text ${JSON.stringify(String(e.search).slice(0, 120))} ${n > 1 ? `occurs ${n} times; include more of the surrounding lines so it is unique` : "does not occur; copy it exactly from the file as shown"}`, modelMs };
    }
    next.set(rel, out);
  }
  for (const [rel, text] of next) writeFileSync(join(WORK, rel), text); // the private copy, not the real src
  return { summary: String(answer.summary ?? "").slice(0, 200), modelMs };
}

async function fallbackPath(ctx, onChild) {
  const session = await runClaude({
    cwd: join(WORK, "src"), // the private copy: its edits reach the real src only after the checks
    args: claudeArgs({ prompt: `App root: ${WORK}\n${ctx.text}`, systemPrompt: FALLBACK_SYSTEM, model: cfg.model }),
    env: claudeEnv({ baseUrl: cfg.baseUrl, token: cfg.apiKey, model: cfg.model, effort: cfg.effort }),
    timeoutMs: SESSION_TIMEOUT_MS,
    onChild,
  });
  if (!session.ok) return { failed: session.error, transport: /took longer than|API Error|ECONN|fetch failed|timed? ?out|overloaded|5\d\d/i.test(session.error) };
  const at = session.text.lastIndexOf("RESULT:");
  const result = at === -1 ? undefined : parseJsonObject(session.text.slice(at));
  if (result?.ok === false) return { refused: String(result.reason ?? "not an actionable change").slice(0, 200) };
  return { summary: String(result?.summary ?? "").slice(0, 200) };
}

/** A wh-question with no change verb in it: never worth a model call, and never to be guessed at. */
export function isQuestion(text) {
  const t = text.trim().toLowerCase();
  if (/\b(change|make|set|rename|replace|turn|swap|remove|hide|delete|add|move|put|increase|decrease|bigger|smaller|should (say|be|read))\b/.test(t)) return false;
  return /^(so |and |but |um+ |uh+ |hey |ok(ay)? )*(why|how come|how do|how does|how can|where|what|when|who|which)\b/.test(t);
}

const MAX_FAST_CALLS = 3;

/** What the phone shows on a refusal: one short sentence. The full reason stays in the log. */
function shortReason(text) {
  const first = String(text).trim().split(/(?<=[.!?])\s/)[0] ?? "";
  return clip(first);
}

/** What the phone shows after "Done: " or on a refusal: never more than 70 characters. */
function clip(text, max = 70) {
  const t = String(text).replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1);
  return `${cut.slice(0, cut.lastIndexOf(" ") > 40 ? cut.lastIndexOf(" ") : cut.length).replace(/[ ,;:.]+$/, "")}…`;
}

/** Does the sentence stop in mid-air ("I want the balance numbers to", "make this button")? */
export function looksCutOff(text) {
  const t = String(text).trim().toLowerCase();
  if (!t) return true;
  if (undoCount(t) > 0) return false; // "undo that" is a whole request
  const w = t.replace(/[^a-z0-9' ]+/g, " ").trim().split(/\s+/);
  if (/^(to|the|a|an|and|or|but|be|is|are|of|with|in|on|at|for|from|into|as|it's|should|must|can|will|than)$/.test(w.at(-1))) return true;
  return w.length <= 2 && !/^(undo|revert|go back|roll ?back)/.test(t);
}

/** One ticket, start to finish. Returns the final bug.fix fields. `progress(text)` tells the phone why it is taking longer. */
export async function fixTicket(bug, progress = () => {}, signal = NEVER) {
  const before = readSrc(APP);
  prepareWork(before);
  let child;
  try {
    return await fixTicketOnce(bug, progress, signal, before, (c) => (child = c));
  } catch (error) {
    if (!(error instanceof Amended)) throw error;
    child?.kill("SIGKILL");
    revert(before); // the private copy; the real src was never written
    log(`  stopped: a fuller transcript arrived (nothing had reached the app)`);
    return { aborted: true };
  }
}

async function fixTicketOnce(bug, progress, signal, before, onChild) {
  const t0 = Date.now();
  /** Await something, but stop at once if the ticket is amended meanwhile. */
  const guard = async (promise) => {
    const out = await Promise.race([promise, signal.promise]);
    if (out === ABORT || signal.aborted) throw new Amended();
    return out;
  };
  const history = loadHistory();
  let ctx = buildContext(bug, before, history);
  const steps = []; // what was tried, for the log line
  log(`  request ${JSON.stringify(ctx.transcript.slice(0, 120))}; selected [${ctx.ids.join(", ")}]${ctx.ids.length ? "" : `; word matches [${ctx.maybe.join(", ")}]; text matches [${ctx.textHits.join(", ")}]`}; files [${ctx.files.join(", ")}]; history ${recent(history).length} (located in ${Date.now() - t0} ms)`);
  const fail = (summary, path) => ({ state: "failed", summary, files: [], path, steps });
  if (!ctx.transcript) return fail("I didn't hear what to change.", "none");
  if (isQuestion(ctx.transcript)) return fail("That sounded like a question, so I left the app as it is.", "none");

  /** Check the private copy. Only if it passes is anything written to the real src (and remembered). */
  const settle = async (path, summary, kind = "change", undid = []) => {
    const t1 = Date.now();
    // Edits to the in-page client (elements.json too) are simply dropped: they would reload the phone.
    for (const rel of changedSince(before)) {
      if (!isClientCode(rel)) continue;
      if (before.has(rel)) writeFileSync(join(WORK, rel), before.get(rel));
      else rmSync(join(WORK, rel), { force: true });
    }
    const problem = await guard(verify(before));
    const ms = Date.now() - t1;
    if (problem) {
      revert(before);
      steps.push(`${path}: discarded before it reached the app (${problem}), verify ${ms} ms`);
      log(`  ${path}: discarded, never written to the app — ${problem}`);
      return { problem };
    }
    const changes = commit(before);
    steps.push(`${path}: ok, verify ${ms} ms`);
    const shown = clip(summary);
    remember(history, bug, ctx, changes, shown, kind, undid);
    return { final: { state: "done", summary: shown, files: changes.map((c) => c.file), path, steps } };
  };
  const tryUndo = async (entries, summary, path) => {
    const r = restoreFrom(entries, before);
    if (!r.ok) {
      steps.push(`${path}: not possible (${r.why})`);
      return { why: r.why };
    }
    const out = await settle(path, summary, "undo", entries.map((e) => e.seq));
    return out.final ? out : { why: out.problem };
  };

  let feedback = "";
  let why = "";

  // 1. Plain "undo": no model at all.
  const n = undoCount(ctx.transcript);
  if (n > 0) {
    const entries = undoable(history, n, bug.sessionId ?? null);
    if (entries.length === 0) return fail("There is nothing from this session to undo.", "undo");
    const u = await tryUndo(entries, entries.length === 1 ? `Reverted: ${clip(entries[0].summary, 58)}` : `Reverted the last ${entries.length} changes`, "undo");
    if (u.final) return u.final;
    why = u.why;
    feedback = `They asked to undo ${entries.map((e) => `#${e.seq}`).join(", ")}, but the files could not be restored automatically (${u.why}). Do not answer with "undo": write edits that put the old lines of those changes back into the files as they are now.`;
    log(`  undo: not possible automatically (${u.why}); asking the model`);
  }

  // 2. Fast path: up to three direct calls (a second or third only to correct the first).
  let calls = 0;
  const extra = [];
  while (USE_FAST && calls < MAX_FAST_CALLS) {
    calls++;
    const out = await guard(fastPath(ctx, before, feedback, signal));
    const tag = `fast#${calls} ${out.modelMs} ms`;
    if (out.refused) {
      steps.push(`${tag}: refused (${out.refused})`);
      return fail(shortReason(out.refused), "fast");
    }
    if (out.need) {
      const add = out.need.filter((f) => before.has(f) && !ctx.files.includes(f) && !isClientCode(f));
      steps.push(`${tag}: asked for ${out.need.join(", ")}`);
      if (add.length === 0) feedback = `The files you asked for (${out.need.join(", ")}) do not exist or are already shown. Answer with edits to the files shown, or refuse.`;
      else {
        extra.push(...add);
        ctx = buildContext(bug, before, history, extra);
        feedback = `You asked for ${add.join(", ")}: shown above now. Answer with edits.`;
      }
      continue;
    }
    if (out.undo) {
      const entries = out.undo.map((seq) => history.find((e) => e.seq === seq && !e.undone)).filter(Boolean).sort((x, y) => y.seq - x.seq);
      const u = entries.length ? await tryUndo(entries, out.summary || `Reverted: ${clip(entries[0].summary, 58)}`, "fast-undo") : { why: `no change with number ${out.undo.join(", ")} is still in effect` };
      if (u.final) {
        steps.unshift(`${tag}: undo ${out.undo.map((x) => `#${x}`).join(", ")}`);
        return u.final;
      }
      why = u.why;
      feedback = `Automatic undo was not possible (${u.why}). Do not answer with "undo" again: write edits that put the old lines back into the files as they are now, or refuse.`;
      steps.push(`${tag}: undo not possible (${u.why})`);
      continue;
    }
    if (out.transport) {
      // MiniMax itself is not answering (every hedged call failed or timed out): say so now,
      // rather than try twice more and then start a session on the same model.
      steps.push(`${tag}: ${out.why}`);
      log(`  ${tag}: model not answering — ${out.why}`);
      return fail(NOT_ANSWERING, "outage");
    }
    if (out.retry) {
      revert(before);
      why = out.why;
      steps.push(`${tag}: unusable (${out.why})`);
      log(`  ${tag}: unusable — ${out.why}`);
      feedback = `Your previous answer could not be used: ${out.why}. Answer again with that corrected.`;
      continue;
    }
    steps.push(tag);
    const done = await settle("fast", out.summary || "Done.");
    if (done.final) return done.final;
    why = done.problem;
    feedback = `Your previous edits were applied and then taken back because the app no longer passed its checks: ${done.problem}. Answer again with edits that avoid this.`;
  }

  // 3. The full session, only when the fast path could not do it.
  if (USE_FALLBACK) {
    progress("This one needs a closer look, a few more seconds…");
    log(`  fast path could not do it (${why || "off"}); starting the full session`);
    const t1 = Date.now();
    const out = await guard(fallbackPath(feedback ? { ...ctx, text: `${ctx.text}\n\nNOTE FROM THE RUNNER: a quick first attempt failed: ${why}\n` } : ctx, onChild));
    steps.push(`session ${Date.now() - t1} ms`);
    if (out.refused) return fail(shortReason(out.refused), "session");
    if (out.failed) {
      revert(before);
      why = out.failed;
      if (out.transport) {
        log(`  session: model not answering — ${out.failed}`);
        return fail(NOT_ANSWERING, "outage");
      }
    } else {
      const done = await settle("session", out.summary || "Done.");
      if (done.final) return done.final;
      why = done.problem;
    }
  }
  log(`  gave up: ${why}`);
  return fail("Sorry, I couldn't make that change safely.", "none");
}

// --- hub connection and queue -------------------------------------------------

let ws;
const queue = [];
const seen = new Set();
let busy = false;

function send(msg) {
  const full = { ts: Date.now(), ...msg };
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(full));
  else pending.push(full); // delivered after the reconnect, so a result is never lost
}
const pending = [];

const reported = new Set(); // tickets we already answered done/failed
let current = null; // { id, signal } of the ticket in hand
const LATE_FINAL_MS = Number(process.env.PINEO_LATE_FINAL_MS || 4000); // the hub amends a ticket up to this long after it closed
const MAX_WAIT_MS = 2500;
const said = (bug) => (bug.transcript ?? []).map((t) => (typeof t === "string" ? t : t?.text)).filter(Boolean).join(" ").trim();

/** Wait for a fuller copy of this ticket, but only while the hub could still send one. True if it came. */
async function waitForMore(bug, receivedAt, signal) {
  const until = Math.min(Date.now() + MAX_WAIT_MS, (bug.closedAt || bug.submittedAt || receivedAt) + LATE_FINAL_MS + 300);
  const ms = until - Date.now();
  if (signal.aborted) return true;
  if (ms < 50) return false;
  await Promise.race([signal.promise, sleep(ms)]);
  return signal.aborted;
}

async function drain() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    let { bug, receivedAt } = queue.shift();
    send({ type: "bug.fix", bugId: bug.id, state: "working" });
    log(`ticket ${bug.id}: working`);
    let out;
    let rounds = 0;
    let waitedOut = false; // already waited the full time for more and nothing came
    for (;;) {
      const signal = makeSignal();
      current = { id: bug.id, signal };
      const restart = (why) => {
        bug = signal.bug;
        rounds++;
        log(`ticket ${bug.id}: ${why}; starting again with ${JSON.stringify(said(bug).slice(0, 120))}`);
      };
      // A sentence that stops in mid-air: its end is probably still on the way.
      if (rounds < 3 && looksCutOff(said(bug))) {
        log(`  ${JSON.stringify(said(bug).slice(0, 80))} looks cut off; waiting briefly for the rest`);
        if (await waitForMore(bug, receivedAt, signal)) {
          restart("the rest of the sentence arrived");
          continue;
        }
        waitedOut = true;
      }
      try {
        out = await fixTicket(bug, (text) => send({ type: "bug.fix", bugId: bug.id, state: "working", summary: text }), signal);
      } catch (error) {
        out = { state: "failed", summary: "Sorry, something went wrong on my side.", files: [], path: "crash", steps: [] };
        log(`  crashed: ${error.stack ?? error}`);
      }
      if (out.aborted && rounds < 3) {
        restart("amended while in progress");
        continue;
      }
      // About to say "failed": if the hub could still send the rest of the sentence, give it a moment.
      if (out.state === "failed" && out.path !== "crash" && out.path !== "outage" && rounds < 3 && !waitedOut && (await waitForMore(bug, receivedAt, signal))) {
        restart(`was going to answer failed (${out.summary}) but more was said`);
        continue;
      }
      if (out.aborted) out = { state: "failed", summary: "Sorry, I lost track of that one — say it again?", files: [], path: "none", steps: [] };
      break;
    }
    current = null;
    reported.add(bug.id);
    const ms = Date.now() - receivedAt;
    send({ type: "bug.fix", bugId: bug.id, state: out.state, summary: out.summary, files: out.files, ms });
    log(`ticket ${bug.id}: ${out.state === "done" ? "✓ done" : "⚠ failed"} in ${ms} ms via ${out.path}${rounds ? ` (restarted ${rounds}x for a fuller transcript)` : ""} — ${out.summary}${out.files.length ? ` [${out.files.join(", ")}]` : ""} {${(out.steps ?? []).join("; ")}}`);
  }
  busy = false;
}

/** `amended`: the hub re-sent the ticket with more of the sentence (or revived one it had cancelled as empty). */
function enqueue(bug, amended = false) {
  if (!bug?.id || reported.has(bug.id)) return;
  if (current?.id === bug.id) {
    if (amended) current.signal.abort(bug);
    return;
  }
  const waiting = queue.find((q) => q.bug.id === bug.id);
  if (waiting) {
    if (amended) waiting.bug = bug;
    return;
  }
  if (seen.has(bug.id) && !amended) return;
  seen.add(bug.id);
  queue.push({ bug, receivedAt: Date.now() });
  if (busy) send({ type: "bug.fix", bugId: bug.id, state: "queued" });
  void drain();
}

function onMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return;
  }
  if (msg.type === "bug.submitted" && msg.bug) return enqueue(msg.bug, msg.amended === true);
  if (msg.type === "hello") {
    // Tickets submitted in the last two minutes while we were away and never picked up.
    const bugs = msg.state?.bugs ?? [];
    for (const bug of [...bugs].reverse()) {
      if (bug?.status === "submitted" && !bug.fix?.state && Date.now() - (bug.submittedAt ?? 0) < 120000) enqueue(bug);
      else if (bug?.id) seen.add(bug.id);
    }
  }
}

async function connectForever() {
  let wait = 500;
  let announced = false;
  for (;;) {
    const closed = await new Promise((resolveOnce) => {
      let opened = false;
      let settled = false;
      const done = (v) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolveOnce(v);
      };
      let sock;
      try {
        sock = new WebSocket(HUB);
      } catch {
        return resolveOnce(false);
      }
      ws = sock;
      // A hub that is not there may never fire close; give up on this attempt after 3 s.
      const timer = setTimeout(() => {
        if (opened) return;
        try {
          sock.close();
        } catch {}
        done(false);
      }, 3000);
      sock.addEventListener("open", () => {
        opened = true;
        log(`connected to the hub at ${HUB}`);
        for (const m of pending.splice(0)) sock.send(JSON.stringify(m));
      });
      sock.addEventListener("message", (e) => onMessage(typeof e.data === "string" ? e.data : ""));
      sock.addEventListener("close", () => done(opened));
      sock.addEventListener("error", () => {
        if (!opened) done(false);
      });
    });
    if (closed) {
      log("hub connection closed; reconnecting");
      wait = 500;
      announced = false;
    } else if (!announced) {
      log(`hub not reachable at ${HUB}; retrying until it is`);
      announced = true;
    }
    await sleep(wait);
    wait = Math.min(wait * 2, 3000);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  log(`history: ${loadHistory().length} earlier change(s) remembered in ${HISTORY_FILE}`);
  log(`edits are checked in ${WORK} before they are written to the app`);
  log(`app ${APP}; model ${cfg.model} (effort ${cfg.effort}); fast path ${USE_FAST ? "on" : "off"}, full session ${USE_FALLBACK ? "on" : "off"}`);
  process.on("SIGINT", () => process.exit(0));
  process.on("SIGTERM", () => process.exit(0));
  void connectForever();
}
