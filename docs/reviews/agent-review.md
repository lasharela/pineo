# Pineo review: coding agent + in-page client

Ten demo risks, most demo-threatening first. Items 1, 2, 3, 4 and 6 were confirmed by experiment or by the real ticket data.

How this was checked: I read all of `agent/hub-runner.mjs`, `agent/lib/common.mjs`, `agent/reset-app.sh` and `mobile-app/src/pineo/{client,circle,index}.ts`, plus part of `Overlay.tsx`. I ran HMR experiments with Playwright and Chrome against a scratch copy of mobile-app on port 4991, with its proxy pointed at a fake hub on 8799. That Vite server is killed. Nothing in the repo was changed (apart from this file), there was no POST to 8787, MiniMax and Deepgram were not called, and `.env` was not opened.

## 1. HIGH, CONFIRMED: a text change can leave the phone stuck on "Working on it…"

- Every text-change ticket also edits `src/pineo/elements.json`. The rule is at `agent/hub-runner.mjs:419`, and the hub-e2e logs show it every time (Send money, Top up goal, Sign out).
- `elements.json` is imported only by `client.ts` (`mobile-app/src/pineo/client.ts:4`), and `client.ts` is not an HMR boundary. So the edit forces a **full page reload**. Confirmed: my window marker was reset after the edit.
- The runner sends `done` about 170–420 ms after writing, right when the phone is reloading. The socket is down, so the message is lost. The hub only sends to sockets that are connected (`hub/server.py` `on_fix`), and `onHello` (`client.ts:316`) ignores fix state.
- On reload, `start()` (`client.ts:186`) restores the saved "working" fix from sessionStorage, and a working pill has no timeout.
- Experiment: when `done` arrived during the reload gap, the pill still said "Working on it…" 20 s later.

Smallest fixes, any of these:

- (a) In `onHello`, apply the newest bug's `fix` from `state.bugs` if it is newer than the current one.
- (b) Don't restore "working"/"queued" from sessionStorage, or give those states a ~20 s timeout.
- (c) Stop the runner editing `elements.json`: remove line 419 and treat `elements.json` as client code. This also gets rid of the reload flash.

Recommended: (a) plus (c).

## 2. HIGH, CONFIRMED: an old tap is passed to the model as "THE ELEMENT THEY SELECTED"

- When nothing is selected, `targetIds()` (`hub-runner.mjs:284`) adds `contextAtOpen.lastTappedElementId`. That is the last element tapped at any point before the ticket opened: the previous ticket's selection, or the tab used to navigate.
- `buildContext` (line 395) then tells the model "THE ELEMENT THEY SELECTED: <that element>", and skips the word-matching candidates and the screen file.
- Real data: the last ticket in `hub/data/bugs.json` selected and then deselected 3 buttons and said "make active navigation element…". History #3 records ids `['home-balance']`, left over from an earlier ticket.
- A likely demo flow: tap the Cards tab, open a ticket, say "make the card number bigger" without tapping. The model is told `tab-cards` was selected.

Fix: in `targetIds`, don't push `last`. At most, list it as a hint ("last tapped before the ticket"), not as the selection.

## 3. HIGH, CONFIRMED: `reset-app.sh restore` would roll back today's client fixes, and history still holds a rehearsal change

- `agent/baseline/mobile-app-src.tgz` is from 14:02. `client.ts` and `Overlay.tsx` were changed at 14:09 (the amend/closing fixes). `reset-app.sh diff` lists `styles.css`, `pineo/Overlay.tsx` and `pineo/client.ts`, so a pre-demo restore puts back the older client.
- `styles.css` still has rehearsal change #3 (tab colour set to `var(--tangerine)`), and `agent/.state/history-3792996033.json` still lists #3 as not undone.
- `undoable()` (line 253) has no age limit, so a first "undo" or "put everything back" at the demo reverts rehearsal changes.
- If restore is run without clearing history, "undo" can't restore the files automatically and falls back to a model call, which is slow and gives an odd result.

Fix: put `styles.css` back, run `reset-app.sh snapshot` now, and delete `agent/.state/history-*.json` at every reset (ideally add that to the restore step).

## 4. MED-HIGH, CONFIRMED: broken edits show on the phone before they are reverted

- The fast path writes to the real src (`hub-runner.mjs:587`) and only then runs verify (tsc takes 0.23 s, kb-check 0.04 s).
- JSX syntax error: Vite's red error overlay appears and clears when the file is reverted. Observed ov=true, then ov=false.
- Runtime error: the whole app goes blank (`#root` has 0 children). React Refresh recovers after the revert, including when combined with the `elements.json` full reload.
- So the flash lasts about 0.3 s on the fast path. On the fallback `claude` session it can last many seconds, because each Edit lands on disk and verify only runs at the end (up to 120 s).

Fixes:

- Set `server.hmr.overlay: false` in `mobile-app/vite.config.ts`. Only do this **before** the demo, because Vite restarts and the phone reloads.
- Better: in the fast path, run tsc on a scratch copy, or at least a per-file `ts.transpileModule` syntax check on the new text in memory, before writing.

## 5. MED: a MiniMax outage means a 2–4 minute spinner

- A transport error returns `retry` (`hub-runner.mjs:566`), and the loop at line 706 tries the fast path again up to 3 times, each with a 40 s `FAST_TIMEOUT_MS`.
- Then it starts the fallback session on the same MiniMax with a 120 s timeout (`agent/lib/common.mjs:61`).
- Only the fallback sends a progress line.
- A first call that hangs and then works is fine thanks to hedging (one test hit 7.9 s).

Fix: if `out.transport`, fail immediately with "The model is not answering, try again". Set `PINEO_FAST_TIMEOUT_MS` to about 15000 and `PINEO_AGENT_TIMEOUT_MS` to about 45000.

## 6. MED, CONFIRMED by reading the source: a "this one only" style change on a shared component is likely to hit every copy, or fail

- The rule (`hub-runner.mjs:416`) says to use an inline style or a dedicated class. But:
  - `LinkRow` and `Accordion` (`mobile-app/src/ui.tsx:77`, `:101`) accept neither `style` nor `className`.
  - `ToggleRow` (`ui.tsx:59`) accepts only `className`.
  - `Tab` (`mobile-app/src/App.tsx:79`) accepts neither.
  - `TxRow` (`mobile-app/src/screens/Activity.tsx:20`) gets its id from `data.ts`, so `locate()` points at `data.ts` and not at the JSX.
- "Make Change PIN red", "make this toggle green" (profile-2fa-toggle), "make this transaction bold" or "make the Home tab bigger" will end one of three ways:
  - TypeScript fails and the fast path retries, which costs seconds.
  - The model edits `ui.tsx` or the shared `.row`/`.switch`/`.tab` CSS, which changes every row, toggle or tab in the app.
  - The ticket ends with "couldn't make that change safely".
- Single elements with their own class or an inline JSX element are fine: the balance card, home-goal-add, profile-logout, the transfer buttons. Rehearse demo lines only on those.

Smallest fix: add `style?: CSSProperties` and `className?` to `LinkRow`, `ToggleRow`, `Tab` and `Accordion`, passed through to the root element.

## 7. MED, PLAUSIBLE (iOS only, not verified on a device): scrolling may not work in selector mode

- In `move()` (`mobile-app/src/pineo/circle.ts:253`), small finger jitter before the hold timer completes returns true, and touchmove then calls `preventDefault` (`circle.ts:302`).
- On iOS WebKit, `preventDefault` on the first touchmove cancels scrolling for the whole gesture. A slow drag that starts with a move under 12 px won't scroll; only a quick flick will.
- Check on the phone: open a ticket, then slowly drag the Home list.

If it's broken: only `preventDefault` once `armed`, and never on the jitter moves.

## 8. MED: after the phone sleeps, the app can be stuck in selector mode or miss messages, with no reconnect

- There is no heartbeat and no `visibilitychange`/`pageshow` handler (`client.ts:201–246`). iOS often leaves the WebSocket half-open after sleep, and `onclose` doesn't fire.
- The client keeps showing "connected" and loses `bug.submitted`, `bug.fix` and `assist`.
- If a ticket was open, the `pineo-ticket` class stays on, which disables all buttons (`pineo.css:255`), and nothing closes it.

Fix: on `visibilitychange` to visible, close the socket and reconnect. `onHello` already resyncs open/closed state.

## 9. LOW-MED (mainly while you rehearse and edit code): `revert()` and `verify()` treat any change under src as the agent's

- If someone saves a file during a ticket, verify sees "edited Pineo client code" and `revert()` (`hub-runner.mjs:52`) silently wipes that person's edit.
- A human edit outside `src/pineo` gets recorded into history by `remember()` (line 152) and is later undone by "undo".

Fix for today: don't edit src while the runner is busy.

## 10. LOW: "undo everything" undoes up to 10 changes

- "Undo everything", "put it all back" and "turn everything back" undo up to 10 changes (`hub-runner.mjs:201`). Together with item 3, that includes rehearsal changes.
- "Delete everything" is caught: removing elements fails the id check (`pineoIds`/kb-check) and is reverted. The worst case is hiding things through a style edit.

## Checked and fine

- **Fast-path guards:** only existing `src/` files, no `..`, no `src/pineo` except `elements.json`. Multi-file edits are applied in memory and then written synchronously.
- **Fallback session:** limited to src via `--restricted` (the flags exist in claude 2.1.323).
- **Revert:** complete, and it recovers cleanly from syntax and runtime breaks, including during a full reload.
- **Amended tickets:** stale model answers are never written; an amend during verify triggers revert and restart.
- **Queue:** serial, so no concurrent tickets; revert is synchronous.
- **Id/kb check:** catches added or removed ids.
- **Undo:** all-or-nothing, with `reverseHunk` when the file has moved on.
- **tsc:** fast (0.23 s).
- **HMR:** the `window.__pineo` guard prevents duplicate clients; edits to pineo `.ts` files force a full reload; `data.ts`, CSS and screen edits hot-swap without a reload.
- **Selector mode:** selections are cleared on `bug.opened`, and nav chrome (tabs, bell) stays usable.
- **Assist:** navigates to the right screen, expands accordions (via `data-pineo-collapsed` and the `pineo:expand` event) and scrolls the element into view, falling back to an instant scroll. A missing or renamed id shows "I couldn't find that here." after at most 2.5 s; kb-check keeps ids in sync.
- **Latency:** typically 2–5 s on the fast path and about 0.2 s for plain undo. The wait for a cut-off sentence adds up to 2.5 s.

## Experiment scripts

The scripts were kept in the reviewer's scratch folder, not in the repo.

- `exp.mjs`: what Vite does after an `elements.json` edit, a syntax error, a runtime error, a `data.ts` edit and a CSS edit.
- `exp2b.mjs`: `done` lost during the reload, so the pill stays on "Working on it…".
- `exp3.mjs`: recovery when a broken edit coincides with the `elements.json` full reload.
