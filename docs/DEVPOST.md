# Pineo — Devpost copy

**Tagline:** Review an app with your wrist and your voice. Point at what is wrong, say it, and watch the source change.

## Inspiration

AI can build a screen in seconds. Reviewing it is now the slow part. You notice ten small things, and for each one you put the phone down, go to a laptop, describe where the thing is, and wait. We wanted the review to happen where the product is: in your hand. The phone's screen is busy showing the app, so the controls had to live somewhere else. A wristband gives two things a phone cannot give without covering the app: a gesture you can make while holding it, and a microphone that is always in the same place.

## What it does

You hold a phone running the app under review and wear the Vexo Open band on the same wrist.

- Rotate your wrist twice: a debugging session starts and the band's microphone turns on.
- Ask "where can I block the card?": the app goes to the right screen, scrolls, and pulses the element.
- Rotate once: a ticket opens. Tap, long-press, or draw a circle around the elements you mean, and say what is wrong or what to change.
- Say "next issue", pause for five seconds, or rotate once: the ticket closes. MiniMax writes it up as a bug or an improvement.
- A coding agent on MiniMax edits the app's source. The change appears on the phone by hot reload, usually in a few seconds. "Turn it back" undoes it.
- Rotate twice: the session ends and the microphone turns off.

A dashboard shows the session, every gesture, the live transcript, the tickets and the agent's progress as they happen.

## How we built it

- **Band process (Python).** The organisers' kit streams the gyro at 50 samples a second and, during a session, the microphone as Opus over Bluetooth. A small detector turns wrist rotations into "double" and "single". The audio goes into a **Pipecat** pipeline: a custom band-audio input, **Deepgram** nova-3 for speech-to-text, a router, and **MiniMax-M3.1-Flash** with function calling.
- **Hub (Python, aiohttp).** One WebSocket server that owns sessions and tickets and passes messages between the band, the phone, the dashboard and the agent.
- **Pineo SDK (TypeScript).** One folder inside the app. Each element carries a `data-pineo-id`, and a JSON file describes every id (102 for our demo bank). That file is all the model knows about the app. There is no screen recording; the phone reports which ids were tapped or circled.
- **Live-fix agent (Node).** Finds the selected elements in the source without a model, asks MiniMax for search/replace edits in one call, checks them in a private copy (types, ids), then writes them to the real source. Vite hot-reloads the phone.
- **Dashboard.** A static page with a debug panel that can drive the whole thing without the band.

## Challenges we ran into

- **Telling a deliberate rotation from a moving hand.** We recorded our own wrist, found that a double rotation is four alternating lobes on one gyro axis in about 0.9 s, and set the thresholds from that data.
- **Knowing when someone has finished talking.** Deepgram cuts a sentence into pieces at short pauses, and the end of a sentence can arrive after the ticket has closed. The hub now waits for speech to stop and attaches late words to the right ticket.
- **MiniMax's slow tail.** Most calls answer in about two seconds; a few take 10–30. The agent sends the same request again after 3 s and 6 s and uses the first answer.
- **Not breaking the app in someone's hand.** A half-written edit must never reach the phone, so every edit is checked in a copy first, and edits to the SDK's own files are refused because they force a full page reload.
- **Changing the design mid-day.** We started with a desktop pointer-and-voice tool and rebuilt around the phone and the wrist once we had the band on.

## Accomplishments that we're proud of

- The whole loop works on the real band and a real phone: gesture, speech, ticket, source edit, hot reload.
- Measured on that setup: "Make numbers green on the balance" changed the phone in 3.2 s; "make them black as it was before" took 1.9 s; "where can I block the card?" was answered in about 2.1 s.
- Undo restores the exact previous files without a model call.
- The microphone is on only during a session, and the band's light shows it.

## What we learned

- A wrist gesture is a good mode switch and a poor pointer. Fingers point; the wrist says "now I am reporting".
- The model needs very little to be useful: a list of element ids with one-line descriptions was enough for both navigation and locating code.
- Speed comes from doing less with the model: locate with grep, ask once, verify with the compiler.
- Fast speech-to-text finals matter more than perfect ones. With endpointing at 100 ms, Deepgram's finals typically arrive 60–180 ms after speech; we join the pieces ourselves.

## What's next

- **Any app, not only ours.** A native or React Native wrapper, and generating the element list automatically, so nothing has to be added by hand.
- **Real screen context.** Send what is actually on screen with the ticket, so the model sees layout problems and not only element ids.
- **Multi-reviewer sessions.** Several people with bands reviewing the same build, with tickets merged in one dashboard.
- Gesture calibration for a new wrist in under a minute.

## Built with

`vexo-open` · `pipecat` · `deepgram` · `minimax` · `python` · `aiohttp` · `bluetooth-le` · `typescript` · `react` · `vite` · `node.js` · `websockets` · `tailscale`
