// Fails when the data-pineo-id values used in the source and the ids in src/pineo/elements.json differ.
// Strict on ids only; labels and other texts may change freely.
// Ids must be written as literals: data-pineo-id="x" on the JSX element, or pineo("x") for the few data-driven rows.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "src");
const kbPath = join(src, "pineo", "elements.json");

const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (p !== join(src, "pineo")) walk(p);
    } else if (/\.(tsx?|jsx?)$/.test(name)) files.push(p);
  }
})(src);

const ID = /(?:data-pineo-id\s*=\s*|\bpineo\()\{?\s*["']([^"']+)["']/g;
const inSource = new Map();
let dynamic = 0;
for (const f of files) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(ID)) {
    if (!inSource.has(m[1])) inSource.set(m[1], relative(root, f));
  }
  dynamic += (text.match(/data-pineo-id=\{/g) ?? []).length;
}

const kb = JSON.parse(readFileSync(kbPath, "utf8"));
const errors = [];
const seen = new Set();
const SCREENS = new Set(["home", "cards", "transfer", "activity", "profile", "*"]);
for (const e of kb) {
  if (seen.has(e.id)) errors.push(`duplicate id in elements.json: ${e.id}`);
  seen.add(e.id);
  for (const k of ["id", "screen"]) {
    if (typeof e[k] !== "string" || !e[k].trim()) errors.push(`${e.id}: missing "${k}"`);
  }
  if (!SCREENS.has(e.screen)) errors.push(`${e.id}: unknown screen "${e.screen}"`);
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(e.id ?? "")) errors.push(`${e.id}: id must be kebab-case`);
}
for (const [id, file] of inSource) if (!seen.has(id)) errors.push(`in source (${file}) but not in elements.json: ${id}`);
for (const id of seen) if (!inSource.has(id)) errors.push(`in elements.json but not in source: ${id}`);

if (errors.length) {
  console.error(`kb:check FAILED (${errors.length})`);
  for (const e of errors) console.error("  - " + e);
  process.exit(1);
}
console.log(`kb:check ok — ${seen.size} elements, ${inSource.size} ids in ${files.length} source files (${dynamic} forwarded attributes)`);
