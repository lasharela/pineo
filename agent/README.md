# Pineo coding agent

Turns a spoken ticket from the hub into a live edit of `mobile-app/src`. Vite hot-reloads
the change on the phone a few seconds after the person stops talking.

```sh
agent/run-hub.sh                   # connect to the hub, edit mobile-app/src as tickets arrive
agent/reset-app.sh snapshot        # record mobile-app/src as the clean baseline
agent/reset-app.sh diff            # which files differ from the baseline
agent/reset-app.sh restore         # put src/ back and forget the runner's memory
agent/reset-app.sh clear-history   # only forget the runner's memory
```

Run from `pineo/`. `run-hub.sh` can be started in any order: it waits for the hub and
reconnects by itself. Nothing in `reset-app.sh` runs on its own.

The model is MiniMax (`MiniMax-M3.1-Flash`). The runner needs Node 20 or newer, the app's
`node_modules` (for `tsc`), and, only for the fallback, the `claude` CLI on `PATH`.

## Options and environment

`run-hub.sh [--app <dir>] [--hub <ws url>] [--no-fast] [--no-fallback]`

Read from `pineo/.env` (the real environment wins over the file). Values are never printed
or written anywhere.

| Name | Used for | Default |
|---|---|---|
| `MINIMAX_API_KEY` | Bearer token for MiniMax | none, required |
| `MINIMAX_ANTHROPIC_BASE_URL` | Anthropic-compatible endpoint | `https://api.minimax.io/anthropic` |
| `MINIMAX_MODEL` | model id | `MiniMax-M3.1-Flash` |
| `PINEO_AGENT_EFFORT` | reasoning effort: `low` … `max` | `low` |
| `PINEO_APP_DIR` | the app to edit | `pineo/mobile-app` |
| `PINEO_HUB_WS` | hub address | `ws://127.0.0.1:8787/ws?role=agent` |
| `PINEO_FAST_TIMEOUT_MS` | limit for the fast path's model call | `15000` |
| `PINEO_HEDGE_MS` | when to send the same request again | `3000,6000` |
| `PINEO_AGENT_TIMEOUT_MS` | limit for the fallback session | `45000` |
| `PINEO_LATE_FINAL_MS` | how long the hub may still amend a closed ticket | `4000` |
| `PINEO_AGENT_STATE` | memory and the private copy of the app | `agent/.state` |

## What happens to one ticket

```
hub ──bug.submitted──▶ hub-runner.mjs ──locate──▶ one MiniMax call ──▶ edits in a private copy
 ▲                                                                            │ tsc + id checks
 └──────bug.fix working / done / failed◀── copy changed files into mobile-app/src ◀┘
```

1. **Arrival.** `bug.submitted {bug}` is answered at once with `bug.fix {state:"working"}`
   (`"queued"` if another ticket is in hand). Tickets are handled one at a time, in order.
2. **Locate, without the model** (a few milliseconds).
   - *Selected elements:* the ones still selected when the ticket was submitted, most recent
     first. A tap with `selected:false` takes one out again; a circle selects everything
     inside it. Nothing else counts: the element tapped before the ticket opened is passed
     to the model only as a weak hint.
   - *Nothing selected:* the words are matched against the labels, keywords and
     descriptions of the elements on the current screen ("switchers" finds the toggles on
     Cards). If the request is about what the app says (a name, an amount, a label), the
     source is searched for that text: quoted words, numbers, names, the word after "not" or
     "from", and for "my name is …" the user fields in the data file. Letters spelled out by
     speech-to-text are joined ("l a s h a, Lasha" becomes "Lasha").
   - The prompt gets the spoken words, what was selected, each element with `file:line`,
     the matching source lines, the whole text of the files involved (the screen, its
     stylesheet, the app stylesheet, small shared modules such as `ui.tsx`, the data file
     for content requests) and the recent history.
3. **Fast path.** One direct call to MiniMax returns one JSON object: search/replace edits
   (several, in several files), or a refusal, or a request for one more file, or the number
   of an earlier change to undo. An edit is applied only if its search text occurs exactly
   once. If the answer cannot be used or fails the checks, the model is told why and
   answers again, up to three calls.
4. **Check before anything reaches the app.** Edits are made in a private copy of the app
   (`agent/.state/overlay-*`). There the runner runs `tsc --noEmit` and the app's
   `scripts/kb-check.mjs`, and checks that no `data-pineo-id` was added, removed or renamed,
   that nothing under `src/pineo/` changed, that edited CSS has balanced braces and edited
   JSON parses. Only when all pass are the changed files copied into `mobile-app/src`. A
   broken edit never reaches Vite or the phone. A TypeScript error that was already in the
   app before the edit is ignored; a new one fails the edit.
5. **Fallback session.** Only when the fast path could not do it: the same prompt goes to
   a headless `claude` session running on MiniMax, working in the private copy, then the
   same checks. The phone is first sent `working` with "This one needs a closer look…".
6. **Answer.** `bug.fix {state:"done"|"failed", summary, files, ms}`. `ms` counts from the
   ticket's arrival. Summaries and refusals are at most 70 characters. A result produced
   while the hub connection was down is sent after the reconnect.

The log line of each ticket ends with the path taken and the time of every step.

### What the runner will not do

- Edit anything under `src/pineo/` (the in-page client, `elements.json` included): that
  would reload the whole page on the phone. Labels in `elements.json` can therefore go
  stale after a text change; ids stay in step.
- Guess a fix for a question, a remark or a complaint with no stated change. A correction
  of what the app shows ("my name is Lasha, not Noa", "the rent should be 1,450") counts as
  a request, also when the sentence is cut off after the clear part.
- Run commands. The fast path only produces text edits. The fallback session has file
  tools only (`--restricted --tools Read,Edit,Write,Grep,Glob`), no shell, web or MCP,
  confined to the private copy.

### Memory and undo

The runner keeps its last ten changes in `agent/.state/history-*.json` (one file per app
folder, survives a restart): what was said, what was selected, the summary, and the exact
before and after of every file it wrote. The five most recent go into every prompt, so
"make it darker" or "same for the other buttons" build on what was just done.

- **Plain undo, no model call (about 0.2 s):** "undo", "undo that", "turn it back", "put it
  back", "change it back", "revert that", "go back", with "the last two" or "everything".
  The saved before-content is restored, newest change first.
- **Undo in other words** ("turn colors back"): the model names the change by number and
  the runner restores it.
- If a file was also edited by someone else since, only the lines that change touched are
  put back, when they are still there exactly once. Otherwise the model gets the diff.
- Undo only reaches changes made in the same hub session (`bug.sessionId`). Older ones are
  shown to the model as from an earlier session.
- Every undo is all-or-nothing and passes the same checks. `reset-app.sh restore` and
  `clear-history` delete the history.

### Amended tickets (late finals)

The hub re-sends `bug.submitted {bug, amended:true}` when the end of a sentence arrives
after the ticket closed. A queued ticket is replaced by the fuller copy. A ticket in hand
is stopped at once and started again with the full text; nothing of the stopped attempt
is written, and the ticket still gets exactly one `done` or `failed`. A first copy already
marked `amended` is a normal new ticket. Once a ticket is answered, later copies are
ignored.

Before answering `failed`, and before starting on a sentence that stops in mid-air ("I
want this button to"), the runner waits for a fuller copy: at most 2.5 s, and never past
the hub's late-final window.

### When MiniMax is slow or down

The same request usually takes 2 s but now and then 10 to 30 s. So if there is no answer
after 3 s the same call is sent again, after 6 s a third time, and the first answer wins.
If every call fails or 15 s pass, the ticket is answered `failed` with "The model is not
answering, try again in a moment." and no session is started.

### Your own Claude Code is not touched

The fallback session gets `CLAUDE_CONFIG_DIR=agent/.claude-home` and its own environment
(inherited `ANTHROPIC_*`, `CLAUDE_*` and key variables are dropped, then
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and the model variables are set for that
process only). Nothing is written to `~/.claude`.

## MiniMax facts (international platform, read 2026-11-14)

- Anthropic-compatible base URL `https://api.minimax.io/anthropic` (messages at
  `/anthropic/v1/messages`), `Authorization: Bearer <key>`. Models `MiniMax-M3.1-Flash` and
  `MiniMax-M3`. Tools and streaming are supported, `tool_choice` only `auto`, `temperature`
  is ignored. https://platform.minimax.io/docs/api-reference/text-anthropic-api
- OpenAI-compatible base URL `https://api.minimax.io/v1`.
  https://platform.minimax.io/docs/api-reference/text-openai-api
- Claude Code on MiniMax: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL` and
  the three `ANTHROPIC_DEFAULT_*_MODEL`, with `CLAUDE_CODE_EFFORT_LEVEL`.
  https://platform.minimax.io/docs/m-plan/claude-code — the runner sets the same variables
  on the child process instead of in `~/.claude/settings.json`, with effort `low`.
- `MiniMax-M3.1-Flash` always thinks; effort defaults to `max`; thinking counts toward
  `max_tokens`.
- No GroupId is needed (confirmed by the runs below).
- The Claude Code page names an "M Plan" key; a pay-as-you-go key works on the same
  endpoint (confirmed by the runs below), which the docs do not say.

## Measured

Ticket submitted to `bug.fix`, real MiniMax, effort `low`. Stand-in hub and a scratch copy
of the app unless said otherwise; ranges are over several runs.

| Spoken | Selected | Time | Path |
|---|---|---|---|
| "change the text on this button to Send money" | one button | 1.0–3.5 s | fast |
| "change the color of this button" | one button | 2.0–4.2 s | fast |
| "make this card blue" | circled card | 2.3–5.7 s, once 11.4 s | fast |
| "make these two buttons the same colour…" | two, a third deselected | 2.9 s | fast |
| "make the switches purple" (Cards) | nothing | 1.8–5.1 s | fast |
| "make it darker" after a colour change | nothing | 1.9–4.5 s | fast, from memory |
| "My name is l a s h a, Lasha. Not" (cut off, wrong screen) | nothing | 2.6 s | fast, text locator |
| "My name is Lasha, not Noa" | nothing | 6.1 s | fast, text locator |
| "change the greeting to Welcome back" | nothing | 2.0 s | fast, text locator |
| "the rent payment should be 1,450 dollars" | nothing | 4.6 s | fast, text locator |
| "turn it back", "undo that", "undo the last two" | nothing | 0.17–0.2 s | undo, no model |
| "turn colors back" | nothing | 1.2–3.7 s | model names it, runner restores |
| half a sentence, the rest 0.7 s later | one button | 3.6 s | restarted once |
| half a sentence, never completed | one button | 4.1 s | failed after waiting |
| model refuses connections | one button | 0.02 s | failed, outage |
| model accepts and never answers | one button | 15.0 s | failed, outage |

Through the real hub code with a real Vite dev server on spare ports
(`test/real-hub.mjs`): 3.0 s, 4.2 s and 4.2 s for the first three rows, with Vite logging
hot updates and no page reload. One ticket was also driven through the running hub and the
real app early on (2.2 s). In the owner's live session with band and phone: 3.2 s for a
colour change, 1.9 s for "make them black as it was before", 12.3 s for a tab colour.

Almost all of the time is the model call; locating takes milliseconds and the checks
0.2–0.5 s. The fallback session alone took 3.5–23 s for the same kind of change.

## Tests

```sh
node agent/test/review-e2e.mjs   # no model: selection, overlay checks, undo scope, outage: 14 checks
node agent/test/hub-e2e.mjs      # real MiniMax, stand-in hub: 34 checks
node agent/test/memory-e2e.mjs   # real MiniMax, stand-in hub: memory, undo, nothing selected: 29 checks
node agent/test/real-hub.mjs     # real MiniMax, the real hub code and Vite on spare ports: 10 checks
```

All of them work on a scratch copy of `mobile-app` with a temporary state directory and
never touch `mobile-app`, port 8787, the hub's data or `agent/.state`. Each exits non-zero
on any failed check. The last output of each is in `agent/test/*.output.txt`
(`live-hub-run.output.txt` is the early run through the live hub).

## Known limits

- "Passes the checks" means TypeScript and the id check. A valid edit to the wrong element,
  or an ugly colour, still reports `done`.
- A person saving the same file during the few seconds of a ticket is overwritten when the
  runner copies its result in.
- A "this one only" style change on a shared component that takes no `style` or
  `className` prop may change every copy, cost a retry, or fail.
- The text locator finds what is written in the source. Text assembled at run time, or a
  word the person pronounces differently from how it is spelled, is not found.
- The model decides what counts as a clear request. It has refused things a person would
  have understood and, once in the fallback session, guessed at a vague remark.
- MiniMax's slow tail: with hedging the worst seen was 11.4 s for a change that usually
  takes 2–3 s.
- The history holds full before/after file contents of the last ten changes
  (`agent/.state`, git-ignored).
- Not tested here: several minutes of continuous use, MiniMax rate limits, `MiniMax-M3`.
