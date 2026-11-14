# Pineo band process

The bridge from the Vexo Open wristband to the Pineo hub. It reads the band's gyro and
microphone over Bluetooth, turns wrist rotations into gestures, and runs the voice pipeline:

    band microphone (Opus) -> PCM -> Pipecat pipeline:
        BandMicInput -> DeepgramSTTService (nova-3) -> UtteranceRouter -> OpenAILLMService (MiniMax, function calling) -> AnswerSink
    band gyro -> gesture detector -> `gesture {double | single}` -> hub
    a second small Pipecat pipeline writes up each submitted ticket (kind, title, summary, severity, elements, steps)

What it sends to the hub and receives from it is in `docs/CONTRACT.md`.

## Run

From the `pineo/` folder, with the hub running (`hub/run.sh`):

    PYTHONPATH=band .venv/bin/python -m pineo_band

It asks you to double-tap the band, connects to the band whose number is in
`PINEO_BAND_NUMBER` and to no other, and keeps running until Ctrl+C.

| Option | What it does |
|---|---|
| `--usb` | The band on the USB cable instead of Bluetooth |
| `--no-autoconnect` | Start idle; connect when the dashboard's Connect button asks |
| `--no-mic` | Gestures only: leave the band's microphone off |
| `--laptop-mic` | Listen with this computer's microphone instead of the band's (also with `--no-band`) |
| `--no-band` | No band: needs `--say`, `--gesture`, `--stdin` or `--laptop-mic` |
| `--say "text"` | No band: a typed utterance through the same pipeline (repeatable) |
| `--gesture NAME` | No band: `double`, `single`, `next`, `open`, `submit`, `cancel` (repeatable, in order with `--say`) |
| `--stdin` | No band: one utterance per line; `/double`, `/single`, `/next` … are gestures |
| `--replay CSV` | Run the gesture detector over a `samples.csv` from `gesture/record.py` and exit (`-v` shows why things were ignored) |
| `--motion-rate HZ` | Gyro samples a second: 10, 25, 50 (default), 100, 208, 416 |
| `--gesture-config FILE` | Detector parameters (default `band/gesture.json`) |
| `--hub URL` | The hub (default `http://127.0.0.1:8787`) |
| `--kb FILE` | An `elements.json` to use when the hub serves none |
| `--sim` | The kit's pretend band. Short tests only: its sample speech is sent to Deepgram |
| `-v` | More on the console, including Pipecat's own log |

Example without a band (questions are answered only during a session, so start one first):

    PYTHONPATH=band .venv/bin/python -m pineo_band --no-band --gesture double --say "where can I block my card?"

Do not run a second copy beside the live one: both would write up the same tickets.

## How it behaves

- **Gestures.** A double rotation and a single rotation are sent raw; the hub decides what
  they mean (session start/end, ticket open/submit).
- **Microphone.** On only while a session is active; the gyro always runs.
- **Questions.** "Where is…", "how do I…", "show me…" are answered during a session by
  highlighting an element from the hub's knowledge base. Room talk gets no answer. While a
  ticket is open only clear navigation questions are answered; the rest is the ticket's text.
- **Voice commands.** Tickets and the session can be driven by voice, with no gesture. Exact
  phrases act at once; natural wording is understood by the model (about 1–3 s):
  - open a ticket (in a session, none open): "open a ticket", "new ticket", "I want to report
    something", "I found a problem". "Open a ticket: the button is too small" opens it and
    files the rest as its first words.
  - submit (ticket open): "close the ticket", "send it", "that's it", "done", "go ahead and
    file it". Cancel: "cancel this ticket", "never mind", "scrap that one".
  - next ticket (in a session): "next issue", also at the end of a sentence.
  - end the session: "end the session", "we're done here".
  The exact phrases must be the whole utterance. The model is only asked about finals of 8
  words or fewer that are worded like an instruction; a sentence that describes the app
  ("the close button is too small") is ticket text. Commands stay out of the ticket.
  A session cannot be STARTED by voice: the microphone is off outside a session, so that
  takes the double rotation (or the dashboard).
- **Closing a ticket.** When one is closing, Deepgram is asked to finalise at once so the
  last words land in the ticket.
- **Hub or network trouble.** The hub client, the band link and Deepgram each reconnect by
  themselves; `band.state` reports the link, the microphone and the speech service.

## Environment

Read from the environment, then `.env` in the folder you start it in, then `pineo/.env`.
A variable that is already set wins. Values are never printed.

| Name | |
|---|---|
| `PINEO_BAND_NUMBER` | Required for every band mode: the number on the band's bag, 1–999. Never guessed, never in the code |
| `DEEPGRAM_API_KEY` | Speech to text. Missing: gestures still work, nothing is transcribed |
| `MINIMAX_API_KEY` | Answers and ticket write-ups. Missing: no answers, plain write-ups |
| `MINIMAX_BASE_URL` | Default `https://api.minimax.io/v1` (must be https) |
| `MINIMAX_MODEL` | Default `MiniMax-M3.1-Flash` |
| `PINEO_HUB_URL` | Default `http://127.0.0.1:8787` |
| `PINEO_MOTION_RATE` | Default 50 |
| `PINEO_DG_ENDPOINTING` | Deepgram pause length in ms, default 100 |
| `PINEO_DG_KEYTERMS` | Extra words for Deepgram, comma separated (default `Brindle, PIN`); the knowledge base's label words are added |
| `PINEO_LLM_EFFORT` | `low` (default), `medium`, `high`, `none` |
| `PINEO_AI_CLOSE` | `1` lets the model close a ticket it judges complete (off by default) |
| `PINEO_KIT_DIR` | Where the Vexo kit is (default `pineo/vexo-open-kit`) |

## The kit

The band library `vexo_open` comes from the organisers' Vexo Open kit and is not on PyPI.
Clone the kit beside this folder as `pineo/vexo-open-kit` (it is git-ignored), or point
`PINEO_KIT_DIR` at it. Nothing is installed from it: the process adds
`<kit>/python` to its import path at start (`bandlink.load_kit`). Its own requirements
(`bleak`, `pyserial`, `av`) are in `band/requirements.txt`.

## gesture.json

Detector parameters, in degrees/second and milliseconds; change them without touching code.
The shipped values were calibrated on one person's wrist (`gesture/recordings/20261114-130023`).
A rotation is a fast twist about the forearm (`gx`) and back. Main fields: `axis`,
`direction`, `threshold_dps` / `release_dps`, `min_peak_ms` / `max_peak_ms`, `return_gap_ms`,
`require_return`, `max_gap_ms` (between the starts of a double's two rotations),
`decision_delay_ms`, `refractory_ms`, `purity`, `single_min_peak_dps`, `double_min_peak_dps`.
The definitions are at the top of `pineo_band/gesture.py`. Check a change with:

    PYTHONPATH=band .venv/bin/python -m pineo_band --replay gesture/recordings/<time>/samples.csv -v

## Tests

    cd band && ../.venv/bin/python -m pytest -q

No network and no band: fakes stand in for the hub, the model and the band.

## Known limits

- The gesture parameters fit one wrist and were never checked against a recording of walking
  or ordinary phone handling, so false triggers there are unknown.
- The Deepgram reconnect fix overrides a private method of Pipecat 1.16.0; pin that version.
- `--laptop-mic` uses `sounddevice`, not Pipecat's local audio transport, and was only
  checked to import and find an input device.
- Answers are text on the phone; nothing is spoken.
- English only; the spoken commands are English phrases.
- Model answers usually take 1–3 seconds.
