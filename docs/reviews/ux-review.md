# Pineo UI/UX review (pre-demo)

Reviewer: review-ux. Method: a throwaway hub on 127.0.0.1:8797 with a scratch data dir and idle timers at 600 s. Headless Chrome, phone at 402×812 @3x with touch, dashboard at 1440×900 and 1920×1080, driven with `/api/debug/*`. Checked against the ui-ux-pro-max checklist. Nothing was sent to the live hub on 8787.

All 9 must-fix and 6 nice-to-have items below take under 20 minutes each.

Screenshots folder (written as `SHOTS/` below): screenshots were kept in the reviewer's scratch folder, not in the repo.

## Must fix before demo

### 1. Phone, ticket open: the dock hides the content being pointed at

The "Listening…" pill plus captions sit over the bottom ~30% of the screen, which is where the Savings goal / Add money card is. When an element under the dock is selected, its green outline and label paint over the captions and the tab bar. The marks render after the dock and so sit on top of it.

Screenshots:
- `SHOTS/phone-20-long-captions.png`
- `SHOTS/phone-11b-after-circle.png` (the "Lisbon trip pot" outline covers the Listening pill)

Fix in `mobile-app/src/pineo/pineo.css`:
```css
.pineo-dock, .pineo-top { z-index: 2; }                 /* marks and picked outlines stay below */
html.pineo-ticket main.scroll > * { padding-bottom: 260px; }  /* lets the presenter scroll any element above the dock */
```

### 2. Captions are clipped mid-line at the top (phone and dashboard)

The first visible line is sliced in half ("button is too small to hit."). The cause is `overflow: hidden` combined with bottom alignment.

Screenshots:
- `SHOTS/phone-09-captions-final.png`
- `SHOTS/phone-12-closing.png`
- `SHOTS/dash-09-reporting-captions.png`
- `SHOTS/dash-12-closing.png`

Phone, `mobile-app/src/pineo/pineo.css`:
```css
.pineo-captions {
  max-height: 4.3em;
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 1.4em);
  mask-image: linear-gradient(to bottom, transparent 0, #000 1.4em);
}
```
In `mobile-app/src/pineo/Overlay.tsx`, change `s.finals.slice(-2)` to `s.finals.slice(-1)`.

Dashboard, `dashboard/style.css`:
```css
.live-transcript {
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 45%);
  mask-image: linear-gradient(to bottom, transparent 0, #000 45%);
}
```

### 3. Wrong instruction in the dashboard copy

`dashboard/app.js` line 145 says "No tickets yet. Rotate the wrist twice to report one." In v3, a double rotation starts or ends a session and a single rotation reports.

Screenshot: `SHOTS/dash-03-session-live.png`

Replace the string with:
```js
session ? 'No tickets yet. Rotate once to report one.' : 'No tickets yet. Rotate twice to start a session.'
```

### 4. Session colour differs between the two surfaces

On the phone a session is blue (`#2f5bea`). On the dashboard it is green, the same green used for "Ticket submitted", "Fixed live" and done. Judges cannot tell "session running" from "something succeeded".

Screenshots: `SHOTS/dash-03-session-live.png`, `SHOTS/dash-13-ticket-submitted.png`, `SHOTS/phone-03-session-live.png`

Add at the end of `dashboard/style.css`:
```css
.live.session .state-line { background: #2f5bea; animation: none; }
.live.session { background: linear-gradient(90deg, rgba(47, 91, 234, .09), transparent 60%); }
.group.now { color: #2f5bea; }
.group.now i { background: #2f5bea; }
```
Keep green only for sent, fixed and done. Setting `animation: none` also removes the infinite session pulse, which moves for the whole demo and pulls attention from the red Reporting state.

### 5. Agent progress never reaches the dashboard's big state pill

While the agent works and finishes, the hero still says "Session live" or "Ticket submitted". "Agent working… 0:07" and "Done in 6.2 s" appear only in a box inside the detail panel, at roughly a third of the hero's size. The gesture → ticket → agent → change chain loses its last step when seen from 2 m.

Screenshots: `SHOTS/dash-16-fix-working.png`, `SHOTS/dash-17-fix-done-1920.png`

Fix in `dashboard/app.js`, in `renderLive()`, before the `else if (session)` branch:
1. Find the newest bug whose `fix.state` is `working`, or whose fix is `done`/`failed` with `updatedAt` (or the time it arrived) under 8 s ago.
2. Set `label` and `timer`:
   - working: `label = 'Agent fixing…'`, `timer = mmss(now() - fix.startedAt)`
   - done: `` label = `Fixed live · ${secs(fix.ms)}` ``
   - failed: `label = 'Fix failed'`
3. Set `sub = fix.summary`.
4. Toggle the class `fixing` (amber) for working, and `done` (green, already exists) for done.

Add to `dashboard/style.css`:
```css
.live.fixing .state-line { background: #a35f00; color: #fff; }
```
About 15 minutes. This is the single biggest legibility win.

### 6. Truncated fix messages on the phone

The two-line clamp cuts the main payoff: "Done: … placed directly und…". The failed state reads "Couldn't change that yet — The date comes from data.ts and the test suite failed after t…". That text is jargon, it is cut off, and it has a capital letter after the dash.

Screenshots: `SHOTS/phone-28-safearea-done-long.png`, `SHOTS/phone-18-fix-failed.png`

Changes:
- `pineo.css`: `.pineo-fix-text { -webkit-line-clamp: 3; }`
- `Overlay.tsx`, failed branch: show only `Couldn't change that — see the dashboard`, without the summary.
- Ask the coding agent for `bug.fix` summaries under about 70 characters.

### 7. The previous ticket's captions stay under "Ticket sent", "Done" and "Couldn't change"

The old words remain for up to 4.5 s under the new status, so two messages compete.

Screenshots: `SHOTS/phone-18-fix-failed.png`, `SHOTS/phone-28-safearea-done-long.png`

Fix in `Overlay.tsx`:
```ts
const captionsLive = !!s.bug || s.closing || (!s.flash && !s.fix && Date.now() - s.captionAt < 4500);
```

### 8. Small white-on-green text fails contrast

Measured contrast:

| Pair | Ratio | Where it is used |
|---|---|---|
| `#fff` on `#0a9d5c` | 3.51:1 | phone selection labels (12px), "Ticket sent" and "Done" pills |
| `#0a9d5c` on `#fff` | 3.51:1 | "Selected: …" line (13px) |
| dashboard `--faint #8892a0` on `--bg` | 2.89:1 | live-transcript hint, interim words, empty states, times |

Changes:
- `pineo.css`: `--pineo: #087f4a;` (about 5:1)
- `dashboard/style.css` `:root`: `--faint: #6b7583;`

### 9. Selection labels are cut off

The label for "Send shortcut" shows as "Send s…" because its max-width is the element's own width.

Screenshot: `SHOTS/phone-07-ticket-3sel.png`

Fix in `pineo.css`:
```css
.pineo-mark span { max-width: min(260px, calc(100vw - 24px)); }
```

## Nice to have

### 10. Gesture chip is unreadable from 2 m

It is 13px and it is the only on-screen sign of what the band did ("Single rotation → ticket opened").

Add to `dashboard/style.css`:
```css
.gesture-chip { font-size: 18px; padding: 6px 14px; }
.gesture-chip.fresh { background: var(--text); color: #fff; border-color: var(--text); }
```

### 11. Mixed vocabulary

The UI mixes Recording, Reporting and Listening; report and ticket; Submitted and sent. Changes in `dashboard/app.js`:

| Line | Current | Change to |
|---|---|---|
| 35 | `'Recording'` | `'Reporting'` |
| 35 | `'Submitted'` | `'Sent'` |
| 101 | `'Submitting…'` | `'Closing…'` (matches the phone) |
| 105 | `'Ticket submitted'` | `'Ticket sent'` |
| 105 | `'Report cancelled'` | `'Ticket cancelled'` |
| 200 | write-up placeholder | `'The write-up appears after the ticket is sent.'` |
| 201 | `'Writing up the report…'` | `'Writing up the ticket…'` |
| 328 | `'This report is speech only.'` | `'This ticket is speech only.'` |

The ticket counts also disagree while a ticket is open: the header says "TICKETS 1" while the session divider and the sub-line both say "0 tickets". Make the divider and the sub-line say "0 sent".

### 12. Phone hint wraps to two clumsy lines

Current text: "Tap or hold to select · it closes by itself when you are done".

Screenshot: `SHOTS/phone-05-ticket-open-0sel.png`

Change it in `Overlay.tsx` to `Tap or circle what you mean`.

### 13. "Session ended · 3 tickets" chip runs into the "Brindle" name

It will overlap longer screen titles such as "Send money".

Screenshot: `SHOTS/phone-22-session-ended.png`

In `Overlay.tsx`, render the `sessionEnded` state inside `.pineo-dock` (at the bottom, like "Ticket sent") instead of `.pineo-top`.

### 14. Dashboard phone drawing: point labels are tiny and overlap

The "Send shortcut" label sits on box 1's border, and labels render at about 12px.

Screenshots: `SHOTS/dash-11-reporting-with-points.png`, `SHOTS/dash-16-fix-working.png`

Changes:
- `dashboard/style.css`:
  ```css
  .pt-label { font: 700 26px var(--sans); paint-order: stroke; stroke: #fff; stroke-width: 6px; }
  ```
- `dashboard/app.js`: change the `pt-dot` radius `r="15"` to `r="20"`.
- Detail panel order: Fix → What was said → Summary → Elements → Steps. At 1440×900 the spoken transcript is currently below the fold.
  Screenshots: `SHOTS/dash-18-fix-failed.png`, `SHOTS/dash-22-session-ended.png`

### 15. Assist "not found" bubble and the idle dot

- The "not found" bubble has a green (success) border.
  Screenshot: `SHOTS/phone-04b-assist-none.png`
  Fix in `pineo.css`: `.pineo-bubble[data-side="none"] { border-color: #e9c46a; }`
- The grey 7px "band connected" dot at the top centre of the idle phone looks like a stray pixel. Remove it, or show it only when debug is on.
  Screenshot: `SHOTS/phone-01-idle-home.png`

## Works well (keep)

- The red "Reporting…" hero
- Ticket cards entering with a glow
- The drawn circle stroke
- Selection outlines (unmistakable)
- The post-fix element flash on the phone
- The "added after closing" amend badge
- Session grouping in the ticket list
- Low battery shown as "9 % low" in red
- Safe-area insets respected (top 62 / bottom 34 emulated)

## App content (probably planted demo bugs, not Pineo issues)

- The "Add money" button overlaps the savings-goal text.
- "Block card" says "Card blocked" while its toggle is off.

## Top suggestions for how the demo reads

a. **Do #5.** The hero pill should walk through Session live (blue) → Reporting (red) → Ticket sent (green) → Agent fixing 0:07 (amber) → Fixed live · 6.2 s (green), so judges watch one spot.

b. **Keep the target element out from under the dock.** Before the demo, scroll the target into the top half of the phone screen, or apply #1's padding.

c. **Show the spoken quote under the ticket title** in the detail panel, as a big italic line (the first final transcript). That makes the "said aloud → ticket" link visible without scrolling.

d. **Make the gesture chip from #10 large and inverted for 2.6 s after each rotation.** It is the only on-screen proof that the band did something.

e. **Run the dashboard at 1920×1080, or zoom it to 110–125% on the projector.** At 1440 the detail column is cramped and titles wrap to four lines (`SHOTS/dash-22-session-ended.png`).

## Screenshot index

All files are in `SHOTS/` (full path at the top of this file). There are about 60:

- **Phone, idle and screens:** `phone-00`…`02` (idle, each of the five screens, top and bottom)
- **Phone, session and assist:** `phone-03` (session live), `phone-04` (assist bubble), `phone-04b` (assist with no match)
- **Phone, ticket open:** `phone-05`/`06`/`07` (0, 1 and 3 selections), `phone-08`/`09` (captions), `phone-10` (circle being drawn), `phone-11`/`11b` (after the circle)
- **Phone, closing and fix:** `phone-12` (closing), `phone-13` (ticket sent), `phone-15`/`16`/`17`/`18` (fix queued, working, done, failed), `phone-19` (closed as empty)
- **Phone, other:** `phone-20` (long captions), `phone-22`/`23` (session ended), `phone-24`/`25` (second session), `phone-26`…`29` (home-screen mode with safe areas)
- **Dashboard:** `dash-*` files match the same states; the variants ending `-1920.png` are 1920×1080.
