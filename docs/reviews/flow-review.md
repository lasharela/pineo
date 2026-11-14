# Flow review: hub/ and band/ (state machine, timing, failure handling, gestures, pipeline, contract)

Reviewer: independent, read-only (this file is the only write). Date: 2026-11-14, a few hours before the demo.
Both test suites pass (hub 39, band 88). Experiments ran on throwaway hubs (in-process aiohttp, other ports);
the live hub/band/agent were only read (`GET /api/state`). `band/pineo_band/cli.py` and `bandlink.py`
changed during the review (mic-only-in-session); the new versions were reviewed.
Experiment scripts (`e1.py <scenario>`, `stall3.py`) were kept in the reviewer's scratch folder, not in the repo.

Ranked, most demo-threatening first.

---

## 1. Band battery is at 14%

- **What happens:** the live hub's `band.battery` is 14. If the band dies on stage, gestures and the mic stop together.
- **Where:** live `GET /api/state` → `band: {connected: true, listening: true, battery: 14, transport: "ble"}`.
- **Sure / how:** sure; read from the live hub.
- **Fix:** charge the band now; check the dashboard just before going on.

## 2. Short questions or filler right after a ticket closes are added to that ticket (and can revive an empty one)

- **Sequence:** a ticket closes (submit, idle, or empty-cancel) → within 4 s (`AFTER_CLOSE_SECONDS`) the presenter says
  something under 5 words or starting lower-case, e.g. "Where is my card?", "Where's the transfer button?",
  "Show me my statements.", "Thanks.", "Okay."
- **Outcome:** the band treats it as the rest of the closed ticket's sentence and sends `transcript {final, bugId: closedId}`.
  The hub adds it as an amendment (`amended: true`) and sends `bug.submitted {amended: true}` again to band and agent,
  so the agent may re-run. The question is never answered.
  If the closed ticket had been cancelled as empty ("Closed — nothing captured"), it is **revived** as a submitted ticket
  whose only text is "Okay." / "Thanks.", and the coding agent tries to act on it.
- **Where:** `band/pineo_band/voice.py:201-206` (continuation test), `band/pineo_band/brain.py` `is_continuation`
  (`len(words) < 5` is enough), hub `hub/server.py:704-709` (`_late_target`), `hub/server.py:734-747` (`_amend`, revive).
- **Sure / how:** sure. Both sides read; ran `brain.is_continuation` / `looks_like_navigation`: "Where is my card?",
  "Where's the transfer button?", "Show me my statements.", "Thanks." all return continuation=True (the first three also nav=True).
- **Smallest fix:**
  - band, `voice.py:202`: add `and not brain.looks_like_navigation(text)` to the continuation condition (questions go to the assistant).
  - hub, `_amend`: revive a cancelled-empty ticket only if the text has ≥ 3 words (or skip revive entirely); optionally ignore
    amendments of < 3 words that are pure filler.

## 3. The band process exits on any reconnect error other than "not-found-nearby"

- **Sequence:** the BLE link drops → the kit's auto-reconnect gives up → `BandLink.run` loops to `connect_patiently` →
  `Band.connect` raises something else, e.g. `VexoError("timeout")` from `get_info()` on a marginal link
  (kit `vexo-open-kit/python/vexo_open/band.py:316-322` re-raises), or a bleak error wrapped differently.
- **Outcome:** `connect_patiently` re-raises (`band/pineo_band/bandlink.py:54`, only `not-found-nearby` is retried) →
  `link.run` ends with an exception → `amain` sets `stop`, `wait_for(runner)` raises → `code = 1`, voice and hub client
  are stopped, the process exits (`band/pineo_band/cli.py:~245-256`, the `runner`/`waiter` block).
  The presenter sees "band disconnected" and no more speech; the process must be restarted and the band double-tapped.
- **Sure / how:** sure, by reading both the kit and our code. Not reproduced (would need the real band).
- **Smallest fix:** in `connect_patiently`, retry on every `VexoError` except `wrong-band`, `bad-arg`, `no-bleak`
  (log it, sleep `RETRY_SECONDS`), e.g. `if getattr(problem, "code", None) in ("wrong-band", "bad-arg", "no-bleak") or (tries and attempt >= tries): raise`.

## 4. Deepgram gives up for good after ~10 s of network loss

- **Sequence:** venue Wi-Fi drops for ~10 s → Pipecat's Deepgram service reconnects with backoff (min 4 s); each failed
  handshake has 0 s uptime → after 3 quick failures in a row `QuickFailureTracker` says give up →
  `push_error(force_treat_as_permanent=True)` and the connection loop returns.
- **Outcome:** `PipelineWorker`'s default `ProcessorUnusablePolicy.CONTINUE` keeps the pipeline running with a dead STT:
  no captions, no ticket text, no questions, no "next issue". Gestures still work. Nothing in our code notices
  (the error goes upstream from the STT; `UtteranceRouter` is downstream and never sees it); only a Pipecat log line on stderr.
  Recovery = restart the band process (= re-pair the band).
- **Where:** `.venv/.../pipecat/services/deepgram/stt.py:675-739` (`_connection_handler`), `.../pipecat/utils/network.py`
  (`QuickFailureTracker(min_stable_duration=5, max_consecutive_failures=3)`, `exponential_backoff_time` min 4 s),
  `.../pipecat/pipeline/worker.py:1640-1670`; ours: `band/pineo_band/voice.py` `Voice._deepgram`.
- **Sure / how:** sure, by reading Pipecat 1.16.0 source. Not reproduced (would call Deepgram).
- **Smallest fix:** in `Voice._deepgram`, after creating the service:
  `from pipecat.utils.network import QuickFailureTracker; stt._quick_failure_tracker = QuickFailureTracker(max_consecutive_failures=10**6)`
  (it then retries every ≤10 s for ever). Optionally log a visible "SPEECH SERVICE DOWN" from `worker.event_handler("on_pipeline_error")`.

## 5. Double then single (or double then "next") inside the closing window: the session does not end as intended

- **Sequence A:** session with a ticket open → double rotation (end session) → hub sends `bug.closing`, waits for speech
  (up to 4.5 s) → a single rotation arrives in that window.
  **Outcome:** `ticket.open` settles the old ticket → `_announce_closed` ends the session (`session.ended`) → then
  `_open_ticket` sees no session and **starts a new session** and opens a ticket. A single rotation outside a session
  should have been ignored.
- **Sequence B:** double (end session, ticket open) → a "next issue" final arrives during the closing window.
  **Outcome:** `_next` sets `session_ending = False`; the ticket settles, a new ticket opens, the session never ends.
- **Where:** `hub/server.py:464-488` (`on_gesture` action table), `hub/server.py:528-529` (`_next` clears `session_ending`),
  `hub/server.py:653-658` (`_announce_closed`), `hub/server.py:445-446` (`_open_ticket` starts a session).
- **Sure / how:** both confirmed on a throwaway hub (`e1.py double_then_single`, `e1.py next_then_double`):
  A ends with a new `session.started` + `bug.opened`; B ends with the session still live and a new ticket open.
- **Smallest fix:** in `on_gesture`, if `self.session_ending` and `name == "single"` → `action = None` (ignored);
  in `_next`, if `self.session_ending` → return ignored (do not clear it).

## 6. A second band client overwrites the band status (and duplicates write-ups)

- **Sequence:** while the live band process is connected, someone runs any `--no-band` invocation
  (`--say`, `--gesture`, `--stdin`, `--laptop-mic`), e.g. a quick test before the demo.
- **Outcome:** it sends `band.state {connected: false, transport: "none"}` at start and again at exit; the hub stores it as
  THE band state and broadcasts it → phone and dashboard show "band disconnected" while the real band is fine, until the real
  band happens to send another state (battery change). The test process also receives `bug.submitted` and runs its own MiniMax
  write-up → duplicate `bug.enrich` for the same ticket.
- **Where:** `hub/server.py:411-420` (`on_band_state` overwrites unconditionally), `band/pineo_band/cli.py` `follow_session`
  (`hub.send("band.state", connected=False, ...)` when there is no link) and the `finally` block (`hub.send("band.state", connected=False, ...)`).
- **Sure / how:** confirmed on a throwaway hub (`e1.py second_band`): app and dashboard got `band.state connected:false`
  after a real `connected:true`.
- **Smallest fix:** operational — do not run band-side tests while the live band runs. Code — in `on_band_state`, ignore
  `connected: false` while `len(self.clients["band"]) > 1` and `self.band["connected"]` is true.

## 7. A slow or hung MiniMax call stalls questions for minutes and mismatches answers

- **Sequence:** a MiniMax stream hangs or is very slow.
- **Outcome:** the OpenAI client Pipecat builds has the SDK defaults: 600 s read timeout, 2 retries. The router's 25 s
  watchdog (`LLM_TIMEOUT`) frees `router.current`, but the `OpenAILLMService` processor is still blocked inside the old request,
  so every later question's `LLMContextFrame` queues behind it; answers that come back > 10 s after sending are dropped silently
  (`LATE_SECONDS`). When the old stream finally ends, `AnswerSink` calls `router.finished(router.current, ...)` — which is now a
  *newer* request — so that one is marked finished with the wrong text (its held words are released into the ticket).
  Tickets are less affected: each gets the plain fallback write-up after 25 s.
- **Where:** `band/pineo_band/voice.py:345-355` (`_dispatch`/`_give_up`), `voice.py:535-542` (`AnswerSink` uses `router.current`),
  `voice.py:594-601` (`_minimax`); `.venv/.../openai/_constants.py:7-8` (`DEFAULT_TIMEOUT = 600`, `DEFAULT_MAX_RETRIES = 2`),
  `.venv/.../pipecat/services/openai/base_llm.py` `create_client` (no timeout passed).
- **Sure / how:** by reading; not reproduced (would call MiniMax).
- **Smallest fix:** in `Voice._minimax`, after building the service: `svc._client = svc._client.with_options(timeout=15, max_retries=0)`.
  Optionally, in `AnswerSink`, finish only if the frame's request is the current one (keep the request on the sink when dispatching).

## 8. "Next issue" spoken without a pause before it is missed

- **Sequence:** the presenter says "…the button should be green next issue" fluently; Deepgram delivers one final with no
  punctuation before "next issue".
- **Outcome:** `trailing_next` needs a sentence mark before the phrase, `spoken_command` needs the phrase alone → no command.
  The ticket stays open; because the presenter keeps talking, the idle timer keeps resetting and the next issue's description
  goes into the same ticket.
- **Where:** `band/pineo_band/brain.py:220-231` (`_NEXT_AT_END`, `trailing_next`), `voice.py:208-219`.
- **Sure / how:** ran `trailing_next("The button should be green next issue")` → None; with ". Next issue." it works.
  How often Deepgram omits the punctuation here is unverified (with 100 ms endpointing a pause usually splits it into its own final, which works).
- **Smallest fix:** let `_NEXT_AT_END` also match without the sentence mark when the phrase is `next (issue|bug|ticket|feedback)`
  (not "next one", which is ambiguous); the hub already strips `NEXT_PHRASE` without punctuation.

## 9. Gesture false triggers are untested; a double (= session end) is the easiest to trigger by accident

- **Replay results:** `--replay gesture/recordings/20261114-130023/samples.csv` → 6 events (3 double, 3 single), all with peaks
  ≥ 995 °/s except one first lobe of 669 °/s; they look intentional. Identical events when the recording is decimated to 50 Hz
  (both phases) and 25 Hz. The other recordings: `20261114-125127` looks simulated (constant gx = 30), `20261114-125940` is empty.
- **The gap:** in 130023 the gyro is active (> 100 °/s) < 1% of the time outside the 6 gestures: there is **no walking or
  phone-handling data**, so false positives from ordinary handling cannot be judged from the recordings.
- **Asymmetry:** a single needs a first lobe ≥ 700 °/s (`single_min_peak_dps`), a double only ≥ 350 °/s per lobe
  (`threshold_dps`). So the session-ending double is the easiest gesture to trigger accidentally (e.g. turning the phone to show
  the audience twice within 0.9 s).
- **Shake guard hole:** with `require_return: true`, `count` is the number of *returned* rotations; a 3-lobe shake where one
  lobe has no turn-back has `count == 2` → "double" (`band/pineo_band/gesture.py:232-251`); the "3 or more is shaking" rule
  looks at `count`, not `len(self._group)`.
- **Slow double:** if the second rotation starts > 900 ms (`max_gap_ms`) after the first, the first is forgotten and it reads as a
  single (in a session: opens or submits a ticket). Natural doubles in the recording were 0.41–0.48 s apart, so margin is fine.
  A double started within 800 ms (`refractory_ms`) after a single decision loses its first rotation.
- **Sure / how:** replays run; logic read.
- **Smallest fix:** record ~2 min of walking + showing the phone with `gesture/record.py` and replay it before the demo.
  Code: for `count == 2` require the weaker first lobe ≥ ~500 °/s (all three real doubles pass; weakest 669), and treat
  `len(self._group) >= 3` as shaking regardless of `count`.

## 10. A phone that stops reading freezes band message processing for ~5 s

- **Sequence:** the phone sleeps with its WebSocket still open (TCP alive, not reading) → the hub's send buffer to it fills.
- **Outcome:** the hub processes each band message by awaiting the fan-out (`send()` → `asyncio.gather` with
  `wait_for(ws.send_str, 5)`); the band socket's handler is serialized, so gestures and transcripts wait. Measured: one freeze
  of ~5 s, then sends to the dead socket fail fast and processing catches up. Also, hub shutdown (`_close_sockets`) hung on
  that socket (`ws.close` awaits drain).
- **Where:** `hub/server.py:352-365` (`send`), `hub/server.py:916-929` (handler loop), `hub/server.py:1061-1065` (`_close_sockets`).
- **Sure / how:** measured on a throwaway hub with a raw socket that completed the handshake and never read (`stall3.py`):
  4000 transcripts + a submit, submit processed after 7.4 s, with a ~5 s pause in the middle.
- **Smallest fix:** lower the per-send timeout to ~0.5 s (and close that socket on timeout); in `_close_sockets`, wrap `ws.close` in `wait_for(..., 1)`.

## 11. If the band isn't connected to the hub when a ticket closes, the ticket never gets a write-up

- **Sequence:** the band process restarts (e.g. to recover BLE), or the hub restarts while a ticket is closing → the ticket is
  submitted (idle timer, or silently settled on load) while no band socket is connected.
- **Outcome:** `bug.submitted` to the band is lost; on reconnect `HubState.handle("hello")` only updates `open_bug_id`/session
  and emits no `bug.submitted`, so `write_up` never runs → the dashboard shows the ticket without title / kind / summary.
  On hub restart, a ticket with `closing: true` is settled in `Store._load` without any announcement. The agent catches up from
  `hello` (`agent/hub-runner.mjs:884-888`); the band does not.
- **Where:** `hub/server.py:148-149` (`_load` settles silently), `band/pineo_band/hub.py:63-73` (`hello` handling), `band/pineo_band/cli.py` `on_hub`.
- **Sure / how:** confirmed on a throwaway hub (`e1.py band_drop_mid_ticket`): the ticket was idle-submitted with no band
  connected; band-side catch-up absent by reading.
- **Smallest fix:** on `hello`, the band emits a write-up for bugs with `status == "submitted"`, no `title`, and
  `submittedAt` within the last 2 minutes.

## 12. Lower-risk hub issues

- **Socket leak if `hello` fails:** `await hub.connect(role, ws)` is outside the `try/finally` (`hub/server.py:915`). If sending
  `hello` raises (client gone during the handshake, e.g. a phone on flaky Wi-Fi), the socket stays in `clients` for ever →
  wrong counts on the dashboard; for role `band`, "band disconnected" is never broadcast and `speech_seen` is never reset.
  By reading. Fix: move `hub.connect` inside the `try`.
- **Several app clients:** any extra app tab (e.g. the laptop on :4848) also gets `bug.opened` / `assist` and enters selector
  mode; the single `app_context` alternates between devices, so the LLM answers against the wrong screen and `contextAtOpen`
  may come from the wrong device (`hub/server.py:819-828`). By reading. Fix: close other app tabs on the demo laptop.
- **Cosmetic:** a single during a "next" closing window reports `accepted: false` although a ticket opens (opened by
  `_announce_closed`) (`hub/server.py:484-489`). By reading.

---

## Checked and fine

- **Stuck `speech.active`:** the closing window is capped at 4.5 s (`PINEO_CLOSE_CAP`) and the session still ends
  (throwaway hub, `e1.py stuck`). The 30 s stuck guard works for the idle timer; the band dropping resets the speech flags.
- **Band drop mid-ticket:** the ticket still closes after 5 s idle (tested).
- **Closing state:** cancel during closing, `next` during closing, single during closing, `reset()`, and double with or without
  a ticket all leave no ticket stuck open; idle-watch tasks and the closing poller end cleanly.
- **Late-word routing:** `transcript.bugId` is used consistently on both sides; with `speech` seen, a final without `bugId`
  after a close is not glued on (it goes to the assistant).
- **Contract fields** match on both sides: `gesture {name, confidence}`, `ticket.complete {bugId, reason}`,
  `transcript {text, final, bugId?}`, `speech {active, flushed?}` (flushed omitted when false), `assist {elementId, say, question}`,
  `assist.none {say, question}`, `bug.enrich {bugId, kind, title, summary, severity, elementIds, steps}`,
  `session.started/ended`, `bug.opened/closing/submitted/cancelled`, `hello.state.{session, openBugId, bugs[].closing}`.
- **Hub restart:** the session and the open ticket are restored and the idle clock restarts (stale > 10 min / 3 h rules apply).
- **Hub unreachable from the band:** gestures (3 s), answers (8 s) and write-ups (600 s) are kept and sent on reconnect;
  the send queue is bounded at 200 (oldest dropped).
- **Mic-in-session change:** Deepgram stays connected between sessions (Pipecat sends KeepAlive every 5 s); the mic turns off
  only on `session.ended`, which the hub sends after the closing window, so last words are kept. First ~0.5 s of a session may
  be lost while `configure(audio=True)` runs (minor).
- **Event loop / growth:** no blocking calls in the band's loop (Opus decode and detector feed are cheap; HTTP is async with 4 s
  limits); no unbounded queues besides Pipecat's own audio input queue; router/hub timer tasks are cancelled or end.
  `bugs.json` is 23 KB, so saving on every change is cheap.
- **Gesture detector at production rate:** identical decisions at 100, 50 and 25 Hz on the real recording.
