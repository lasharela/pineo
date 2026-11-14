# Pineo — demo video shot list (about 2 min 30 s)

Every step below uses something that was seen working today. Where a step was verified only through the debug routes and not with the band, it says so.

## Before recording

1. All four processes running (`hub/run.sh`, `pnpm dev` in `mobile-app`, the band process, `agent/run-hub.sh`). Band process log says the band is connected.
2. `agent/reset-app.sh restore` so Brindle is in its clean state (run `snapshot` first if there is no baseline yet). Balance numbers black, Send button reads "Submit transfer".
3. Phone: open `https://<machine>.<tailnet>.ts.net:8443/` (the `tailscale serve` address from the README) on the **Home** screen. No session running.
4. Laptop: dashboard at `http://localhost:8787/dashboard/`, debug panel hidden. Press **Reset all** in the debug panel first if old tickets are listed.
5. Framing: phone in the left hand with the band visible on that wrist; laptop screen with the dashboard behind or picture-in-picture. Record the dashboard as a separate screen capture so it can be cut in.
6. Do one silent dry run of the gestures. Rotations must be brisk: a slow turn is ignored on purpose.

## Shots

| # | Time | Do / say | Phone shows | Dashboard shows |
|---|---|---|---|---|
| 1 | 0:00–0:15 | Voice-over: "AI builds a screen in seconds. Reviewing it is the slow part. This is Pineo: you review an app with your wrist and your voice." Hold up the phone and the band. | Brindle Home, nothing from Pineo visible. | No session. Band connected. |
| 2 | 0:15–0:25 | **Rotate the wrist twice.** Voice-over: "Two rotations start a session. The band's microphone is on only now." | "Session" indicator appears. Close-up of the band: blue light. | Gesture "double → session start". Session card appears. |
| 3 | 0:25–0:40 | Say: **"Where can I block the card?"** | Live caption, then the app switches to Cards, scrolls to **Block card** and pulses it with a short answer. (About 2 s.) | Transcript line; Assist entry with the element. |
| 4 | 0:40–0:50 | Tap the **Home** tab. **Rotate once.** Voice-over: "One rotation opens a ticket. Now a tap selects instead of pressing." | Red frame, "Listening… describe it" pill. | Gesture "single → ticket open". New open ticket. |
| 5 | 0:50–1:10 | **Tap the total balance.** Say: **"Make numbers green on the balance. Next issue."** | Balance outlined green with its name; caption; "Ticket sent"; then "Working on it…" → "Done: …" and the numbers turn green. (3.2 s when measured.) A new ticket is already open. | Ticket 1 submitted with the selected element, then its write-up (improvement) and the agent state working → done. Ticket 2 open. |
| 6 | 1:10–1:30 | Wait for "Done" from shot 5. Then say: **"Turn it back."** Stop talking and hold still. Voice-over after: "No words for five seconds closes the ticket." | Caption; after about 5 s "Ticket sent"; numbers go back to black. | Ticket 2 closed by idle; agent done (undo). |
| 7 | 1:30–1:55 | **Rotate once** (new ticket). Tap the round **Send** tab, scroll to the bottom, **tap "Submit transfer"**. Say: **"Change the text on this button to Send money now."** **Rotate once** to send it. | Selector mode; tab bar still works; button outlined; then "Working on it…" → "Done" and the button reads "Send money now". | Ticket 3 with the tapped element; closed by gesture; agent working → done. |
| 8 | 1:55–2:10 | **Rotate twice.** Voice-over: "Two rotations end the session. The microphone is off again." | "Session ended · 3 tickets". Close-up of the band: one green blink, then dark. | Gesture "double → session end". Session listed with 3 tickets. |
| 9 | 2:10–2:30 | Cut to the dashboard full screen; scroll the three tickets. Voice-over: "Vexo Open band for gestures and the microphone. Pipecat runs the voice pipeline: Deepgram for speech, MiniMax to understand it, and MiniMax again to edit the code. Three changes, no keyboard." | — | The three tickets with transcript, selection, write-up and fix time. |

Alternative for shot 6 (measured on the real setup, 1.9 s): **"Make them black as it was before."** Use it if you would rather show a spoken undo that goes through the model.

Verified with the band and phone: shots 2, 3, 5 and the undo. Shot 7's text change was verified through the running hub with the debug routes (2.2 s) and in the agent's tests; a spoken text change has not yet been run with the band today. Do a take of it first, and if it misbehaves replace it with a second colour change on one element (for example tap the **Submit transfer** button and say "Make this button blue").

## If something misbehaves

| Problem | Do this instead |
|---|---|
| A rotation is not detected | Laptop: dashboard, press `D`, click **Double** or **Single**. The hub treats it exactly like the band's gesture. Keep the camera on the phone. |
| "Next issue" is not recognised | Click **Next** in the debug panel, or rotate once (submits) and once more (opens the next ticket). |
| Speech is not transcribed (dashboard band card says reconnecting, or no captions) | Type the line in the debug panel's transcript box and click **Final**. Or restart the band process with `--no-mic --laptop-mic` and speak to the laptop; gestures still come from the band. |
| The band will not connect | Run without it: `PYTHONPATH=band .venv/bin/python -m pineo_band --no-band --laptop-mic`, and use the **Double / Single** buttons for gestures. Say in the video that the gestures are simulated in this take. |
| A question highlights nothing | Ask one from the list in `docs/MOBILE-DEMO-SCRIPT.md` ("How do I block my card?", "Where can I see my PIN?"). Last resort: debug panel **Assist** with `card-block-toggle`. |
| The fix takes longer than 10 s | Keep rolling and cut the wait in the edit, but do not hide it: show the real time from the dashboard. If it fails, the phone says so; repeat the ticket. |
| The phone is stuck on "Working on it…" | Reload the page on the phone; it returns to the same screen. |
| The app is left in a changed state between takes | `agent/reset-app.sh restore`. |

## Do not show

- The terminal with the band process at start-up if it prints the band number, and anything from `.env`.
- Requests that describe a bug without saying the fix ("the total is wrong"): the ticket is written up, but the agent declines to change anything.
