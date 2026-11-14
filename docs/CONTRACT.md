# Pineo v2 contract (shared by all workers)

Pineo: you hold a phone running a mobile web app, wearing the Vexo Open band on the same wrist.
Rotate the wrist twice to open a ticket, describe it aloud while tapping or drawing a circle around
the elements you mean, rotate once to submit. MiniMax summarises it into a ticket and decides whether
it is a bug or an improvement. There is NO screen recording: the app reports taps and drawn circles.
(A ticket is called `bug` in message and field names for historical reasons.) You can also ask "where is X?" / "how do I Y?" at any
time and the app scrolls to the element and pulses a marker next to it. A dashboard shows bugs live.

## Processes

| Part | Folder | Runs on | Tech |
|---|---|---|---|
| Hub | `hub/` | laptop, port **8787** | Python (aiohttp), in `pineo/.venv` |
| Mobile app "the reviewed app" | `mobile-app/` | phone browser | React + Vite + TS; built files served by the hub at `/` |
| Dashboard | `dashboard/` | laptop browser | static page(s), served by the hub at `/dashboard/` |
| Band process | `band/` | laptop | Python: kit library (BLE) + gesture detector + Pipecat pipeline (Deepgram STT, MiniMax LLM) |

**Live changes are the point of the demo:** the phone loads the app from the Vite DEV server
(`http://<tailscale-host>:4848`, `host: true`, HMR on) so that when the coding agent edits
`mobile-app/src`, the change appears on the phone by hot reload without losing state. Vite proxies
`/ws`, `/api` and `/dashboard` to the hub (port 8787). Phone and laptop are on the same Tailscale
network; Vite must allow the Tailscale host name (`server.allowedHosts`) and HMR must work through it.
The hub also serves the built `dist` at `/` as a fallback.

## WebSocket: `GET /ws?role=app|dashboard|band|agent`

JSON text messages, each `{ "type": string, "ts": epochMs, ...fields }`. The hub is the single
source of truth: it validates, updates state, and fans out. Unknown types are ignored. On connect
the hub sends `hello` with the full current state to that client.

### band → hub
- `band.state` `{ connected, listening, battery?, transport }` — `transport` is `"ble"`, `"serial"`, `"sim"`, `"laptop-mic"` or `"none"` (band process running without a band); `battery` is a percentage; optional `stt`: `"ok"` | `"reconnecting"` (the band process lost Deepgram and is reconnecting: nothing is transcribed meanwhile; absent when no speech service runs); `link`: `"connected"` | `"connecting"` | `"searching"` | `"disconnected"` (`disconnected` = released by hand or not started; the process stays running and does not retry until `band.control {action: "connect"}`); `hint` (only while `searching`): what the person should do, e.g. "Double-tap the band so it blinks blue"
- `gesture` `{ name: "open" | "submit" | "cancel", confidence? }` — hub turns these into bug open/submit
- `transcript` `{ text, final: bool }` — live speech; hub appends finals to the open bug, if any. Optional `bugId` on a final: words that Deepgram delivered up to 4 s AFTER that ticket closed (the rest of a sentence) — the hub should append them to that ticket if it still can, instead of treating them as new speech
- `speech` `{ active: bool, flushed?: true }` — whether the person is speaking right now. `active: true` at the first interim text; `active: false` after 1.2 s without any interim or final. On `bug.closing` (and on any gesture sent while a ticket is open) the band asks Deepgram to `Finalize`, sends the resulting final transcript(s), then `speech { active: false, flushed: true }` = nothing more is coming for this ticket (normally within 1 s). The hub should hold the closing window until `speech.active` is false or `flushed` arrives (max ~4 s). Not sent for typed utterances (`--say`)
- `assist` `{ elementId, say, question? }` — answer to a question (`question` = the utterance it answers): highlight `elementId`, show/speak `say`
- `assist.none` `{ say, question? }` — question understood but no element matches
- `bug.enrich` `{ bugId, kind: "bug" | "improvement", title, summary, severity: "low"|"medium"|"high", elementIds: string[], steps?: string[] }` — MiniMax turns the spoken feedback + interactions into a ticket after submit, and decides whether it is a bug or an improvement

### agent → hub  (the coding agent that changes the app's source live)
- `bug.fix` `{ bugId, state: "queued" | "working" | "done" | "failed", summary?, files?: string[], ms? }` — hub stores it as `bug.fix` and fans out `bug.upsert` to dashboards and `bug.fix` to apps (the app shows "Working on it…" then "Done: <summary>"; the change itself arrives by Vite hot reload)

### hub → agent
- `hello`, and `bug.submitted` `{ bug }` with the full ticket (transcript, interactions, contextAtOpen) the moment it is submitted — the agent does not wait for `bug.enrich`; `bug.upsert` when it is enriched later

### app → hub
- `app.context` `{ screen, visibleElementIds: string[], lastTappedElementId?, viewport }` — sent on every screen change, scroll settle and tap (throttled)
- `app.interaction` `{ bugId, items: Interaction[] }` — what the person did while a ticket is open (NO screen recording, no rrweb). `Interaction` = `{ at, kind: "tap" | "circle" | "screen" | "scroll" | "input", screen, elementId?, label?, elementIds?: string[], rect?: {x,y,w,h}, path?: [x,y][] }`. `circle` = the person drew a loop with a finger around one or more elements to point at them (`elementIds` = elements inside the loop, `path` = the simplified stroke in viewport fractions 0..1 so the dashboard can redraw it)
- `app.assist.shown` `{ elementId, ok: bool }`

### hub → app
- `hello` `{ state }`
- `bug.opened` `{ bugId }` — start capturing interactions, enable circle-drawing, show the "reporting" indicator
- `bug.submitted` `{ bugId }` / `bug.cancelled` `{ bugId }` — stop capturing, flush
- `transcript` `{ text, final }` — to show live captions
- `assist` `{ elementId, say }` — navigate to the element's screen if needed, scroll it into view, pulse a marker next to it
- `band.state`

### hub → dashboard
- `hello` `{ state }` (bugs list, band state, app context)
- `bug.upsert` `{ bug }` — on every change (opened, transcript appended, submitted, enriched)
- `transcript`, `band.state`, `app.context`, `assist`, `gesture` — forwarded for the live view

### hub → band
- `hello`, `app.context` (so the LLM knows the current screen and what is visible), `bug.opened`, `bug.submitted` (with the full bug, to enrich)

## Bug

```json
{ "id": "bug_…", "status": "open|submitted|cancelled", "kind": "" , "openedAt": 0, "submittedAt": 0,
  "transcript": [{ "text": "…", "at": 0 }], "interactions": [ ], "screen": "cards",
  "elementIds": ["card-block-toggle"], "contextAtOpen": { }, "title": "", "summary": "", "severity": "", "steps": [],
  "fix": { "state": "", "summary": "", "files": [], "ms": 0 } }
```

## HTTP (hub)

- `GET /api/state` — same as `hello.state`
- `GET /api/bugs`, `GET /api/bugs/:id`
- `GET /api/kb` — the element knowledge base (below)
- `POST /api/debug/gesture` `{ name }`, `POST /api/debug/transcript` `{ text, final }`, `POST /api/debug/assist` `{ elementId, say }` — drive everything without the band (for tests and as a demo fallback)
- Tickets persist to `hub/data/` (git-ignored).

### Submit: closing window and empty tickets (hub behaviour, owner request)

- On a `submit` gesture the hub sends **`bug.closing` `{ bugId }`** to app and band at once (new message type). The app must leave reporting / circle mode and flush any pending `app.interaction` on `bug.closing`; the verdict follows.
- The hub then waits up to 1.2 s (`PINEO_SUBMIT_GRACE`, seconds), or until the next final `transcript`, whichever is first. A final transcript or interaction that lands in that window is added to the ticket. During the window the ticket is still `open` with `closing: true`; a second `submit` is ignored, a `cancel` cancels it, an `open` settles it immediately and opens a new one.
- Verdict: if the ticket has any final transcript text or any pointing that still stands — a `tap` whose element was not deselected afterwards, any `circle`, or a legacy `input` (`screen`, `scroll` and taps that ended deselected do not count) → `bug.submitted` as before (app; band and agent with the full bug). Otherwise it is cancelled: status `cancelled`, `cancelReason: "empty"`, and `bug.cancelled` `{ bugId, reason: "empty" }` goes to app, band and agent. The agent never gets `bug.submitted` for it. A `cancel` gesture gives `reason: "gesture"`.
- `bug.opened` is sent to app and band before anything else on an `open` gesture; nothing delays it.
- `POST /api/debug/gesture` with `submit` responds after the verdict, so `bug.status` in the response is final.

### Hub implementation notes (additive; nothing above is changed)

- `hello.state` = `{ bugs (newest first), openBugId, band, appContext, assists (newest first, max 30), clients: {app,dashboard,band,agent} }`. `hello` also carries `role`. Apps should resume capturing if `openBugId` is set.
- Extra fields on forwarded messages: `transcript.bugId` (open ticket or null); `gesture.accepted` + `gesture.bugId`; `assist` / `assist.none` carry `assistId` and `question` (the last final transcript within 20 s, or a `question` the band sends itself).
- `assist.none` is forwarded to app and dashboard. `app.assist.shown` and a `clients` `{ clients }` count message go to dashboards. `bug.cancelled` also goes to the band. `bug.upsert` goes to the agent on enrich and on interactions that arrive after submit.
- When the last band socket closes the hub broadcasts `band.state` with `connected: false`.
- Until the write-up arrives, `bug.elementIds` = the elements the person ended up pointing at, recomputed on every `app.interaction`: selected by a `tap` and not deselected afterwards (`selected: false` removes it; a later tap or circle adds it back), or inside a `circle`. It is no longer seeded from `lastTappedElementId`. `bug.enrich.elementIds` replaces the list and later interactions do not change it again.
- A ticket still open when the hub restarts stays open if it is under 10 minutes old, otherwise it is cancelled.
- Extra debug routes: `POST /api/debug/interaction` `{ items, bugId? }` (defaults to the open ticket), `/api/debug/enrich` (same body as `bug.enrich`), `/api/debug/fix` (same body as `bug.fix`), `/api/debug/band` (same body as `band.state`), `/api/debug/reset` (erases all tickets). `/api/debug/assist` without `elementId` acts as `assist.none`. `GET /api/kb?optional=1` returns `[]` instead of 404.
- Dashboard drawing: `Interaction.rect` is read as CSS pixels of `contextAtOpen.viewport` `{ width, height }` (or as 0..1 fractions if every value is ≤ 1.5); `path` is 0..1 fractions as specified.

## Element knowledge base: `mobile-app/src/pineo/elements.json`

Every interactive or notable element in the app carries `data-pineo-id="<id>"`. The same ids are
listed in `elements.json`, which is the only thing the LLM knows about the app:

```json
[{ "id": "card-block-toggle", "screen": "cards", "label": "Block card",
   "description": "Switch that freezes the selected card", "howTo": "Open Cards, pick a card, toggle Block card",
   "keywords": ["freeze", "lock", "block"] }]
```

The hub serves this file at `/api/kb`. A build-time check in `mobile-app` fails if an id is in the
DOM markup but not in the file, or the reverse.

Additions from the mobile-app worker:

- `screen` is one of `home`, `cards`, `transfer`, `activity`, `profile`, or `"*"` for elements that are
  on every screen (the bottom tab bar and the header bell). For `"*"` the app does not navigate.
- `app.context.viewport` is `{ width, height, dpr }` in CSS pixels.
- `app.interaction` details: items are sent about every 0.7 s while a ticket is open (a `circle` is sent
  at once) and once more on submit/cancel; if the socket drops they are kept and sent after reconnecting.
  `rect` and `path` are viewport fractions 0..1. `tap` has `elementId` + `label` (absent when the tap hit
  nothing with an id) and the element's `rect`; `screen` is sent when the ticket opens and on every screen
  change; `input` is sent when a field's value is committed (no value is sent); `scroll` is not sent.
  `circle` has `elementIds` (most specific elements whose centre is inside the loop), `elementId`/`label`
  of the first one, the loop's bounding `rect`, and `path` (≤ 40 points).
- Selector mode: while a ticket is open, taps do not operate the app. A tap selects the nearest marked
  element and sends `tap` with `selected: true`; tapping it again deselects and sends `tap` with
  `selected: false` (treat that as "not meant after all"). `input` is therefore no longer sent. Elements
  with `screen: "*"` (tab bar, header bell) keep working so the person can change screens; they are never
  selected and send no `tap`, only the resulting `screen` item. `bug.cancelled` shows "Closed — nothing captured".
- Long press: resting a finger about 0.35 s on an element selects it (same `tap` item as a tap). If that
  same touch then becomes a circle, the long-press pick is withdrawn (`tap` with `selected: false`) unless
  the loop contains it, so a hold-then-circle can send `tap`, `tap` (off), `circle` in a row.
- On `bug.closing` the app leaves selector mode, sends pending items at once and shows "Closing…" until
  `bug.submitted` ("Ticket sent") or `bug.cancelled` ("Closed — nothing captured") arrives (4 s safety timeout).
- Drawing a circle on the phone: hold the finger still for about a quarter of a second, then drag. A drag
  that moves straight away is a normal scroll. With a mouse, any drag while a ticket is open draws.
- On `hello` the app resumes or ends its ticket from `state.openBugId` / `state.bugs[].status`.
- `bug.fix`: the app shows "Working on it…" for `queued`/`working`, "Done: <summary>" for `done` and a
  calm line for `failed`. On `done` it flashes `elementIds` if the message carries them, otherwise the
  elements tapped or circled in that ticket.
- For the coding agent: every id is a literal on its JSX element — `grep -rn 'data-pineo-id="<id>"' mobile-app/src`
  lands on it (five payment rows are `pineo("<id>")` in `src/data.ts`). Texts are in the same JSX. Colours:
  add an inline `style` on that element, or edit the class in `src/screens/<Screen>.css` / `src/styles.css`
  (palette variables at the top of `styles.css`). Edits under `src/screens` and CSS apply by hot reload
  without losing screen, scroll or typed values; edits to `src/pineo/*` or `src/main.tsx` cause a full
  reload, which still returns to the same screen and scroll position. After `pnpm install` changes
  dependencies, restart the dev server (or touch `vite.config.ts`).

## Rules for every worker

- Work only in your own folder. Do not commit or push. Never print or commit secrets; real keys live only in `pineo/.env` (git-ignored). Env names: `DEEPGRAM_API_KEY`, `MINIMAX_API_KEY`, `MINIMAX_BASE_URL`, `MINIMAX_MODEL`, `PINEO_BAND_NUMBER`.
- The band number comes only from `PINEO_BAND_NUMBER`; never hard-code or guess it (see `vexo-open-kit/AGENTS.md`).
- If you must deviate from this contract, change this file in the same edit and say so in your report.

## v3 interaction model: sessions (owner decision, 13:40 — supersedes the gesture rules above)

**The microphone is ON only during a session** (owner decision, 14:10; this supersedes "listens all the time"): the band process turns the band's sound stream on at `session.started` (or a `hello` showing a session) and off at `session.ended`; the gyro always runs, so the double rotation can start a session. `band.state.listening` says which it is. Questions are therefore answered only during a session. ~~The microphone listens all the time while the band process runs.~~ "Where is…/how do I…/show me…"
questions are answered ALWAYS (in or out of a session, with or without a ticket open).

| Gesture | State | Result |
|---|---|---|
| double rotation | no session | **session starts** (a debugging session) |
| double rotation | session active | **session ends**; an open ticket is settled first (submitted if it has content, else cancelled) |
| single rotation | session active, no ticket | **a ticket opens** ("I am reporting something") — phone enters selector mode |
| single rotation | session active, ticket open | manual submit (fallback; normally the AI closes it) |
| single rotation | no session | ignored |

**Tickets close automatically.** The band process's LLM watches an open ticket and, once the
description is complete (a clear bug report or change request, and the person has stopped adding to
it), sends `ticket.complete`. Safety nets in the hub: a ticket with content and no new speech or
selection for `PINEO_TICKET_IDLE` (default 8 s) is submitted; an empty ticket idle for
`PINEO_TICKET_EMPTY_IDLE` (default 15 s) is cancelled.

Messages:
- band → hub: `gesture { name: "double" | "single" }` (raw; the HUB applies the table — the old names `open`/`submit`/`cancel` stay accepted and act on the ticket directly, for debug routes and spoken commands), and `ticket.complete { bugId, reason }` which the hub treats as a submit of that ticket (goes through the same closing window and empty rule).
- hub → app, dashboard, band, agent: `session.started { sessionId, startedAt }`, `session.ended { sessionId, endedAt, tickets: n }`. `hello.state.session` = `{ id, startedAt } | null`. Every ticket carries `sessionId`.
- Debug: `POST /api/debug/gesture { name: "double" | "single" | "open" | "submit" | "cancel" }`.

Phone: a session shows a calm persistent indicator (e.g. a thin frame + small "Session" chip with a
listening dot) and the app stays fully usable; selector mode and the "Listening… describe it" pill
appear only while a ticket is open. Session end shows a brief summary ("Session ended · 3 tickets").

### v3 as implemented by the hub (additive details)

- `gesture` to dashboards carries `action`: `"session.start" | "session.end" | "ticket.open" | "ticket.submit" | "ticket.cancel" | null` (null = ignored, e.g. a single rotation with no session) plus `accepted`.
- `session.ended` also carries `startedAt`. `tickets` counts submitted tickets only (cancelled / empty ones are not tickets). `hello.state.sessions` lists ended sessions, newest first (max 50): `{ id, startedAt, endedAt, tickets }`.
- Double rotation with a ticket open: `bug.closing` goes out at once, the ticket settles through the normal closing window and empty rule, then `session.ended` follows. The session stays live during that window.
- A single rotation during a closing window settles that ticket at once and opens the next one.
- Old name `open` with no session starts one first (`session.started`, then `bug.opened`). Old names never end a session.
- `ticket.complete` for a `bugId` that is not the open ticket is ignored; without `bugId` it applies to the open ticket. `reason` is stored as `bug.completeReason`. Debug: `POST /api/debug/complete { bugId?, reason? }`.
- `bug.closedBy`: `"gesture" | "complete" | "idle" | "session-end"` says what closed the ticket.
- Idle timers: `PINEO_TICKET_IDLE` (8 s) and `PINEO_TICKET_EMPTY_IDLE` (15 s), in seconds. The clock restarts on any transcript (interim or final) and on any interaction for the open ticket. An idle submit goes through the closing window; an idle empty ticket is cancelled directly with `reason: "empty"`.
- Restart: a live session and its open ticket survive (the idle clock restarts from the moment the hub comes back). A session with no open ticket that is over 3 hours old is ended on start.
- Ticket idle default is now 5 s (`PINEO_TICKET_IDLE`); the empty-ticket timer stays 15 s.
- `gesture { name: "next" }` (spoken "next issue / bug / feedback"; dashboard `action: "ticket.next"`): with a ticket open the hub sends `bug.closing`, waits at most 0.4 s for a late final transcript (instead of 1.2 s), settles the ticket (submitted, or cancelled as empty) with `closedBy: "next"`, then opens a new ticket in the same session (`bug.opened`). With a session and no ticket it opens one; with no session it starts one and opens a ticket. A trailing "next/new issue|bug|feedback|ticket|one" is stripped from the closed ticket's last final transcript, and a final that is only that phrase is not stored in the new ticket if it arrives within 3 s of opening.
- `app.diag { items: [...] }` from role app: each item is appended as one JSON line `{ at, client, item }` to `hub/data/diag.log` (max 200 items per message, 4000 characters per item, rotated to `diag.log.1` at about 5 MB). Never fanned out. `GET /api/debug/diag?n=200` returns `{ lines: [...] }`, the last n lines.

### Closing waits for speech to finish; late finals (hub, 13:55)

- While a ticket is closing (after `bug.closing`), the hub decides only when the person has stopped talking:
  - Band sends `speech`: the window stays open while `speech.active` is true; it ends at once on `speech { active: false, flushed: true }`, otherwise 0.5 s after the later of `speech.active` going false and the last transcript. Hard cap 4.5 s (`PINEO_CLOSE_CAP`). A stuck `active: true` with no transcript for 30 s is ignored.
  - Band sends no `speech` (older band): an interim within the last 1.5 s keeps it open; it ends 0.5 s after a final that follows the last interim; with no speech at all it ends after 1.2 s. Same cap.
  - `next`: same rule with a 0.4 s settle and a 2 s cap. Idle timer: `speech.active` true counts as activity, so a ticket never idle-closes mid-sentence.
- Late finals (amendments): a final that arrives within 4 s (`PINEO_LATE_FINAL`) after a ticket closed is appended to it as `{ text, at, amended: true }`, the ticket gets `amended: true`, and `bug.submitted { bugId, bug, amended: true }` is sent again to band and agent (plus `bug.upsert` to dashboards). Not done if `bug.fix.state` is already `done` or `failed`, or if the ticket was cancelled by a cancel gesture. A ticket that had been closed as empty is revived: it becomes `submitted` and the app also gets `bug.submitted { bugId, amended: true }` after its earlier `bug.cancelled`.
  - Which ticket: `transcript.bugId` if the band sets it (a closed ticket inside the window, or the open one). Without `bugId`: the open ticket if there is one; otherwise the last closed ticket, but only when the band does not send `speech` at all. A band that sends `speech` must mark late words with `bugId`, so that a question asked right after a submit is not glued onto the ticket.
- Tickets carry `closedAt` (ms) and `closedBy`; the dashboard timeline shows how each ticket was closed and marks amended lines.
- Debug: `POST /api/debug/speech { active, flushed? }`; `/api/debug/transcript` accepts `bugId`.

### Flow-review fixes in the hub (14:35)

- Session ending: after a double rotation with a ticket open, `double`, `single`, `next` and `open` are ignored until the ticket has settled and `session.ended` is out (dashboard `gesture` gets `accepted: false, reason: "session ending"`). `cancel` still cancels the ticket; the session then ends.
- Late words: bare filler ("okay", "thanks", "yeah okay") never amends a ticket. A ticket closed as empty is revived only by late words that are three or more words and not filler.
- `band.state` is kept per band connection. Everyone is shown the real wristband's state (transport `ble` or `serial`) when one is connected; a test client with another transport cannot hide or clear it. Optional `band.state.stt` (`"ok" | "reconnecting"`) is passed through.
- A band that connects is handed `bug.submitted { bugId, bug, catchup: true }` right after `hello` for tickets sent in the last 2 minutes that still have no title, so a ticket that closed while the band was away gets its write-up. If the band does its own catch-up from `hello`, a duplicate `bug.enrich` is harmless.
- A client that does not take a message within 1.5 s (a sleeping phone) is dropped and must reconnect; hub shutdown no longer waits on such a socket.

### Band connect / disconnect from the dashboard (owner request, 14:45)

- `POST /api/band/control { action: "connect" | "disconnect" }` → the hub sends `band.control { action }` to the real band process only: a band-role client whose last `band.state` has transport `ble`/`serial` or carries `link` (typed test clients have neither). Response `{ ok, sent, sessionEnded }`; `409 { error: "Band process not running" }` when there is none; `400` for another action.
- On `disconnect` with a session live, the hub first ends the session exactly like a double rotation (an open ticket goes through the closing window and empty rule, then `session.ended`), and only then sends `band.control`. So the band receives `session.ended` before `band.control { action: "disconnect" }` and need not end the session itself.
- The band reports progress in `band.state`: `link` and `hint` are passed through to app and dashboard unchanged.
- Dashboard: "Disconnect" when `link` is connected, "Connect" when disconnected, disabled "Connecting…" with the hint while searching/connecting, "Band process not running" with a disabled button when no band client is connected. With a session live the first click asks "End session and disconnect?" and the second click within 4 s confirms.
