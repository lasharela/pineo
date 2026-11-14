// Shared by hub-runner.mjs and its tests. No dependencies.
import { spawn } from "node:child_process";
import { existsSync, readFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const AGENT_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const PINEO_ROOT = resolve(AGENT_DIR, "..");
/** Private Claude Code config dir: the owner's ~/.claude (login, plugins, MCP, hooks) is never read or written. */
export const CLAUDE_HOME = join(AGENT_DIR, ".claude-home");

/** Non-secret defaults, the same values as pineo/.env.example. */
export const DEFAULTS = {
  MINIMAX_ANTHROPIC_BASE_URL: "https://api.minimax.io/anthropic",
  MINIMAX_MODEL: "MiniMax-M3.1-Flash",
};

/** Parse a dotenv file into an object. Values are never logged by anything in this folder. */
export function parseEnvFile(path) {
  const out = {};
  if (!existsSync(path)) return out;
  for (const raw of readFileSync(path, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (line === "" || line.startsWith("#")) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let value = m[2].trim();
    const quoted = /^(["'])(.*)\1$/.exec(value);
    if (quoted) value = quoted[2];
    else value = value.replace(/\s+#.*$/, "");
    out[m[1]] = value;
  }
  return out;
}

/**
 * The MiniMax settings: the real environment wins over pineo/.env, which wins over the defaults.
 * `source` says where each one came from, (names only, never values).
 */
export function loadConfig() {
  const envPath = join(PINEO_ROOT, ".env");
  const file = parseEnvFile(envPath);
  const pick = (name) => {
    if (process.env[name]) return { value: process.env[name], source: "environment" };
    if (file[name]) return { value: file[name], source: ".env" };
    if (DEFAULTS[name]) return { value: DEFAULTS[name], source: "default" };
    return { value: "", source: "missing" };
  };
  const key = pick("MINIMAX_API_KEY");
  const baseUrl = pick("MINIMAX_ANTHROPIC_BASE_URL");
  const model = pick("MINIMAX_MODEL");
  return {
    envFileExists: existsSync(envPath),
    apiKey: key.value,
    baseUrl: baseUrl.value.replace(/\/+$/, ""),
    model: model.value,
    source: { MINIMAX_API_KEY: key.source, MINIMAX_ANTHROPIC_BASE_URL: baseUrl.source, MINIMAX_MODEL: model.source },
    effort: process.env.PINEO_AGENT_EFFORT || file.PINEO_AGENT_EFFORT || "low",
    timeoutMs: Number(process.env.PINEO_AGENT_TIMEOUT_MS || file.PINEO_AGENT_TIMEOUT_MS || 120000),
  };
}

const STRIP = /^(ANTHROPIC_|CLAUDE_|CLAUDECODE$|MINIMAX_|DEEPGRAM_|OPENAI_|AWS_BEARER_TOKEN_BEDROCK$)/;

/**
 * The environment of the `claude` child only. Everything that could carry the owner's own
 * Claude login or another provider is dropped, then MiniMax's documented variables are set.
 * https://platform.minimax.io/docs/m-plan/claude-code
 */
export function claudeEnv({ baseUrl, token, model, effort }) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) if (!STRIP.test(k)) env[k] = v;
  mkdirSync(CLAUDE_HOME, { recursive: true });
  return {
    ...env,
    CLAUDE_CONFIG_DIR: CLAUDE_HOME,
    ANTHROPIC_BASE_URL: baseUrl,
    ANTHROPIC_AUTH_TOKEN: token,
    ANTHROPIC_MODEL: model,
    ANTHROPIC_DEFAULT_SONNET_MODEL: model,
    ANTHROPIC_DEFAULT_OPUS_MODEL: model,
    ANTHROPIC_DEFAULT_HAIKU_MODEL: model,
    ANTHROPIC_SMALL_FAST_MODEL: model,
    CLAUDE_CODE_EFFORT_LEVEL: effort,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_AUTOUPDATER: "1",
    DISABLE_TELEMETRY: "1",
    DISABLE_ERROR_REPORTING: "1",
  };
}

/** Flags that make `claude -p` a file editor confined to its working directory. */
export function claudeArgs({ prompt, systemPrompt, model, tools = "Read,Edit,Write,Grep,Glob" }) {
  return [
    "-p", prompt,
    "--model", model,
    "--system-prompt", systemPrompt,
    "--restricted", // no Bash/WebFetch, file tools confined to cwd, user/project settings ignored
    "--tools", tools,
    "--permission-mode", "acceptEdits",
    "--permission-prompts", "none", // anything that would prompt is denied
    "--strict-mcp-config", // no MCP servers
    "--safe-mode", // no CLAUDE.md, skills, plugins, hooks
    "--disable-slash-commands",
    "--no-session-persistence",
    "--output-format", "json",
  ];
}

/** Spawn `claude`, collect stdout, enforce a wall-clock limit. Resolves, never rejects. */
export function runClaude({ cwd, args, env, timeoutMs, onChild }) {
  return new Promise((done) => {
    const started = Date.now();
    let child;
    try {
      child = spawn("claude", args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      return done({ ok: false, error: `could not start claude: ${error.message}`, ms: 0 });
    }
    onChild?.(child);
    let out = "";
    let err = "";
    let timedOut = false;
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      done({ ok: false, error: `could not start claude: ${error.message}`, ms: Date.now() - started });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const ms = Date.now() - started;
      if (timedOut) return done({ ok: false, error: `claude took longer than ${timeoutMs} ms`, ms });
      let json;
      try {
        json = JSON.parse(out.trim().split("\n").pop());
      } catch {
        return done({ ok: false, error: `claude exit ${code}: ${(err || out).trim().slice(0, 300)}`, ms });
      }
      if (code !== 0 || json.is_error) {
        return done({ ok: false, error: `claude exit ${code}: ${String(json.result ?? err).slice(0, 300)}`, ms, json });
      }
      done({ ok: true, text: String(json.result ?? ""), turns: json.num_turns, apiMs: json.duration_api_ms, ms, json });
    });
  });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
