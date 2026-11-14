"""Pineo band process. From the pineo folder:

    PYTHONPATH=band .venv/bin/python -m pineo_band              the band over Bluetooth (double-tap it when asked)
    PYTHONPATH=band .venv/bin/python -m pineo_band --usb        the band on the USB cable
    ... --no-mic --laptop-mic                                   gestures from the band, voice from the laptop microphone
    ... --no-band --say "where can I block my card?"            no band: one typed utterance through the same pipeline
    ... --no-band --gesture open --say "the total is wrong" --gesture submit
    ... --no-band --stdin                                       one utterance per line; /open /submit /cancel are gestures
    ... --no-band --laptop-mic                                  no band: the laptop microphone
    ... --replay gesture/recordings/<time>/samples.csv [-v]     run the gesture detector over a recording and exit

The band number comes from PINEO_BAND_NUMBER (environment or pineo/.env) and nowhere else.
"""
import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from . import config, gesture
from .hub import HubClient

GESTURES = ("double", "single", "next", "open", "submit", "cancel")     # the hub applies the session table to double/single


class _Step(argparse.Action):
    """--say and --gesture keep their order on the command line."""

    def __call__(self, parser, namespace, values, option_string=None):
        steps = getattr(namespace, "steps", None) or []
        steps.append(("say" if option_string == "--say" else "gesture", values))
        namespace.steps = steps


def arguments(argv=None):
    p = argparse.ArgumentParser(prog="pineo_band", description="Pineo: band gestures and voice to the hub.")
    p.add_argument("--usb", action="store_true", help="the USB cable instead of Bluetooth")
    p.add_argument("--sim", action="store_true", help="the kit's pretend band (short tests only: its sound goes to Deepgram)")
    p.add_argument("--no-band", action="store_true", help="no band at all: use --say, --gesture, --stdin or --laptop-mic")
    p.add_argument("--no-autoconnect", action="store_true", help="start idle: connect to the band only when the dashboard asks")
    p.add_argument("--no-gyro-log", action="store_true", help="do not write the raw gyro samples to band/logs/")
    p.add_argument("--no-mic", action="store_true", help="leave the band's microphone off (gestures only)")
    p.add_argument("--laptop-mic", action="store_true", help="listen with this computer's microphone instead of the band's")
    p.add_argument("--say", action=_Step, metavar="TEXT", help="no band: a typed utterance (repeatable, in order with --gesture)")
    p.add_argument("--gesture", action=_Step, choices=GESTURES, help="no band: send a gesture (repeatable)")
    p.add_argument("--stdin", action="store_true", help="no band: one utterance per line; /open /submit /cancel are gestures")
    p.add_argument("--hub", metavar="URL", help="the hub (default %s, or PINEO_HUB_URL)" % config.DEFAULT_HUB)
    p.add_argument("--kb", metavar="FILE", help="an elements.json to use when the hub serves none")
    p.add_argument("--motion-rate", type=int, metavar="HZ", help="gyro samples a second: %s (default 50, or PINEO_MOTION_RATE)"
                   % ", ".join(map(str, config.MOTION_RATES)))
    p.add_argument("--gesture-config", metavar="FILE", help="detector parameters (default band/gesture.json)")
    p.add_argument("--replay", metavar="CSV", help="run the detector over a samples.csv from gesture/record.py and exit")
    p.add_argument("--bug-open", action="store_true", help="--replay: start as if a ticket were open")
    p.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)       # now the default
    p.add_argument("--legacy", action="store_true", help="--replay: the old open/submit mapping instead of raw double/single")
    p.add_argument("--language", default="en", help="spoken language for Deepgram (default en)")
    p.add_argument("-v", "--verbose", action="store_true", help="more on the console (and Pipecat's own log)")
    args = p.parse_args(argv)
    args.steps = getattr(args, "steps", None) or []
    if (args.steps or args.stdin) and not args.no_band:
        p.error("--say, --gesture and --stdin need --no-band")
    if args.no_band and (args.usb or args.sim):
        p.error("--no-band does not go with --usb or --sim")
    if args.no_band and not (args.steps or args.stdin or args.laptop_mic):
        p.error("--no-band needs --say, --gesture, --stdin or --laptop-mic")
    return args


def say(text) -> None:
    print("%s  %s" % (time.strftime("%H:%M:%S"), text), flush=True)


def run_replay(args) -> int:
    params = gesture.load_params(args.gesture_config)
    say("detector: %s" % json.dumps(params.__dict__))
    _, lines = gesture.replay(args.replay, params, bug_open=args.bug_open, verbose=args.verbose, raw=not args.legacy)
    for line in lines:
        print(line)
    return 0


def laptop_microphone(voice):
    """This computer's microphone -> the same pipeline input the band uses."""
    import sounddevice
    stream = sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320,
                                        callback=lambda data, frames, when, status: voice.push_pcm(bytes(data)))
    stream.start()
    return stream


async def amain(args) -> int:
    import os
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "WARNING")
    from .voice import Voice                             # Pipecat loads here (a second or two)

    params = gesture.load_params(args.gesture_config)
    detector = gesture.GestureDetector(params)
    detector.raw = True                                  # v3: every double and single goes to the hub, which decides
    hub = HubClient(config.hub_url(given=args.hub), log=say)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            pass
    tasks = [asyncio.ensure_future(hub.run())]
    try:
        await asyncio.wait_for(hub.connected.wait(), 4.0)    # so the app's words can go to Deepgram as key terms
    except asyncio.TimeoutError:
        pass
    if not hub.state.kb and args.kb:
        try:
            hub.state.set_kb(json.loads(Path(args.kb).read_text(encoding="utf-8")))
            say("knowledge base: %d elements from %s" % (len(hub.state.kb), args.kb))
        except (OSError, ValueError) as problem:
            say("Could not read %s: %s" % (args.kb, problem))

    band_mic = not args.no_band and not args.no_mic and not args.laptop_mic
    listen = band_mic or args.laptop_mic
    deepgram = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if listen and not deepgram:
        say("Not listening: DEEPGRAM_API_KEY is not set (in the environment or in pineo/.env). Gestures still work.")
        listen = band_mic = False
    minimax = config.minimax()
    if not minimax["key"]:
        say("MINIMAX_API_KEY is not set: questions are not answered and tickets get a plain write-up.")
        minimax["key"] = "missing"
    from . import brain
    terms = brain.keyterms(hub.state.kb, os.environ.get("PINEO_DG_KEYTERMS", "") or "Brindle, PIN")
    if listen:
        say("Deepgram key terms: %d (%s...)" % (len(terms), ", ".join(terms[:8])) if terms else
            "Deepgram key terms: none (no knowledge base yet; set PINEO_DG_KEYTERMS for extra words).")
    voice = Voice(hub, minimax, deepgram, stt=listen, endpointing=config.endpointing(), language=args.language, log=say,
                  keyterms=terms, ai_close=os.environ.get("PINEO_AI_CLOSE", "").strip().lower() in ("1", "true", "yes", "on"))
    say("closing tickets: say \"next issue\", rotate once, or stop talking (the hub's idle timer)%s."
        % ("; the AI also closes complete ones (PINEO_AI_CLOSE)" if voice.ai_close else ""))
    tasks.append(asyncio.ensure_future(voice.run()))
    await voice.started.wait()

    links = []                                           # the band link, once there is one
    started = {}

    written = set()
    last_state = {"connected": False, "listening": False, "transport": "none", "link": "disconnected"}

    def publish(**fields) -> None:
        """band.state, always whole and truthful: the link, the microphone, and the speech service."""
        last_state.update(fields)
        if links or microphone:           # a typed test run (--say, --gesture, --stdin) has no band state to report: it must
            hub.send("band.state", **dict(last_state, stt=voice.stt_state))    # not overwrite the real band's on the hub

    def follow_session(active: bool) -> None:
        """The microphone is on only during a session: the band's sound stream, the laptop microphone
        and what goes to Deepgram all follow it. Deepgram's connection itself stays open (Pipecat sends
        it keep-alives), so the first words of a session are not lost; with no sound sent, nothing is billed."""
        voice.set_listening(active)
        if active:
            started["at"] = time.monotonic()
        for link in links:
            asyncio.ensure_future(link.set_mic(active))
        if not links:
            publish(connected=False, listening=bool(active and microphone), transport="laptop-mic" if microphone else "none")

    def on_hub(what, payload) -> None:
        if what == "band.control":
            if not links:
                say("band.control %s: this run has no band (--no-band); ignored." % payload)
            elif payload == "connect":
                links[0].connect_now()
            else:
                links[0].release()
            return
        if what == "session.started":
            say("SESSION START (%s). The microphone is on: ask where things are, rotate once to open a ticket, "
                "twice to end the session." % payload.get("id"))
            follow_session(True)
        elif what == "session.ended":
            say("SESSION END (%s): %s ticket(s). The microphone is off." % (payload.get("id"), payload.get("tickets")))
            follow_session(False)
        elif what == "session":
            say("session: %s" % ("active (%s)" % payload.get("id") if payload else "none (the microphone stays off until one starts)"))
            follow_session(bool(payload))
        elif what == "bug.opened":
            say("TICKET OPEN (%s): describe it aloud; \"next issue\", one rotation or a pause closes it." % payload)
        elif what == "bug.closing":
            say("ticket closing (%s): last words are still added." % payload)
            asyncio.ensure_future(voice.flush_speech("ticket closing"))
        elif what == "bug.cancelled":
            voice.router.ticket_closed(payload)
            say("ticket cancelled (%s)." % payload)
        elif what == "bug.submitted":
            if payload["bugId"] in written:               # an amendment or a reconnect: one write-up per ticket is enough
                return
            written.add(payload["bugId"])
            if payload.get("missed"):
                say("TICKET %s was submitted while this process was not connected: writing it up now..." % payload["bugId"])
            else:
                voice.router.ticket_closed(payload["bugId"])
                say("TICKET SUBMITTED (%s): writing it up..." % payload["bugId"])
            asyncio.ensure_future(write_up(payload))

    async def write_up(payload) -> None:
        for _ in range(40):                              # late words still on their way belong in the write-up
            if not voice.router.speech_active:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.7)                         # the app flushes its last taps and circles after the submit
        bug = await hub.fetch_bug(payload["bugId"]) or payload.get("bug")
        if isinstance(bug, dict):
            await voice.write_ticket(bug)

    microphone = None
    hub.listeners.append(on_hub)
    watcher = asyncio.ensure_future(watch_loop(say))
    stt_watcher = asyncio.ensure_future(voice.watch_stt(lambda state: publish()))

    def send_gesture(name, confidence=None, rotations=None, detail="") -> None:
        if hub.state.bug_open:                           # it may close the ticket: get the pending words out first
            asyncio.ensure_future(voice.flush_speech("gesture"))
        went = hub.send("gesture", name=name, confidence=confidence)
        say("GESTURE %s%s%s%s" % (name.upper(), (" (%d rotation%s)" % (rotations, "" if rotations == 1 else "s")) if rotations else "",
                                  ("  [%s]" % detail) if detail else "",
                                  "" if went else "  [hub not connected: sent when it is back, within 3 s]"))

    code = 0
    try:
        if args.laptop_mic and listen:
            say("This computer's microphone is used during a session. What it hears then is sent over the internet to "
                "Deepgram (deepgram.com) and, as text, to MiniMax. Ctrl+C stops it.")
            microphone = laptop_microphone(voice)
        follow_session(bool(hub.state.session))
        if args.no_band:
            if not hub.connected.is_set():
                say("The hub is not connected: nothing reaches the phone or the dashboard.")
            for kind, value in args.steps:
                await step(kind, value, hub, voice, send_gesture)
            if args.stdin:
                say("Type an utterance and press Enter (/open /submit /cancel for gestures, Ctrl+D to end).")
                while not stop.is_set():
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        break
                    line = line.strip()
                    if line.startswith("/") and line[1:] in GESTURES:
                        await step("gesture", line[1:], hub, voice, send_gesture)
                    elif line:
                        await step("say", line, hub, voice, send_gesture)
            if microphone:
                await stop.wait()
            else:
                await asyncio.sleep(4.5)                 # the hub closes a ticket up to 1.2 s after the gesture; the write-up starts 0.7 s later
                if not await voice.wait_idle(40):
                    code = 1
        else:
            from .bandlink import FATAL_CODES, BandLink, GyroDump
            number = config.band_number()
            transport = "sim" if args.sim else "serial" if args.usb else "ble"
            link = BandLink(number, transport, config.motion_rate(given=args.motion_rate), detector,
                            on_gesture=lambda e: send_gesture(e.name, e.confidence, e.rotations, e.detail),
                            on_pcm=voice.push_pcm if band_mic else (lambda pcm: None),
                            on_state=lambda **fields: publish(**dict(fields, listening=fields["listening"] or bool(microphone and voice.mic.listening and fields["connected"]))),
                            mic=band_mic, log=say, verbose=args.verbose)
            if not args.no_gyro_log:
                link.dump = GyroDump(config.REPO_ROOT / "band" / "logs")
                say("every gyro sample is written to band/logs/gyro-<start>.csv (replay it with --replay <file> -v).")
            links.append(link)
            link.want_mic = bool(hub.state.session)
            if args.no_autoconnect:
                link.wanted.clear()
                say("Not connecting to the band until Connect is pressed on the dashboard (--no-autoconnect).")
            waiter = asyncio.ensure_future(stop.wait())
            while not stop.is_set():                     # the band link is restarted for as long as the program runs
                runner = asyncio.ensure_future(link.run(stop))
                await asyncio.wait([runner, waiter], return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    break
                problem = runner.exception() if runner.done() and not runner.cancelled() else None
                if isinstance(problem, config.ConfigError) or getattr(problem, "code", None) in FATAL_CODES:
                    say("%s" % problem)                  # another band answered, or Bluetooth cannot be used at all
                    code = 1
                    break
                say("band: the link ended (%s); starting it again in 2 s. Voice and the hub keep running." % (problem or "no reason given"))
                publish(connected=False, listening=False)
                await asyncio.sleep(2)
            stop.set()
            try:
                await asyncio.wait_for(runner, 6)
            except asyncio.TimeoutError:
                runner.cancel()
            except Exception:
                pass
            waiter.cancel()
    except config.ConfigError as problem:
        say("%s" % problem)
        code = 2
    finally:
        if microphone:
            microphone.stop()
            microphone.close()
        watcher.cancel()
        stt_watcher.cancel()
        for link in links:
            link.dump.close()
        if links or microphone:
            hub.send("band.state", connected=False, listening=False, transport="none", link="disconnected")
        await voice.stop()
        await hub.close()
        for task in tasks:
            try:
                await asyncio.wait_for(task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
    return code


async def watch_loop(log, every=0.05, limit=0.25) -> None:
    """Says so when the event loop that serves the band, the pipeline and the hub stood still:
    if band samples are lost WITHOUT such a line, the cause is the radio link, not this program."""
    loop = asyncio.get_running_loop()
    last = loop.time()
    while True:
        await asyncio.sleep(every)
        now = loop.time()
        if now - last - every > limit:
            log("WARNING: the event loop stood still for %.0f ms." % ((now - last - every) * 1000))
        last = now


async def step(kind, value, hub, voice, send_gesture) -> None:
    if kind == "gesture":
        send_gesture(value)
        await asyncio.sleep(0.5)                         # the hub answers with bug.opened / bug.submitted
    else:
        say('SAID "%s"' % value)
        await voice.say(value)
        await asyncio.sleep(0.3)
        await voice.wait_idle(40)


def main(argv=None) -> int:
    args = arguments(argv)
    config.load_env()
    try:
        if args.replay:
            return run_replay(args)
        return asyncio.run(amain(args))
    except (config.ConfigError, gesture.GestureConfigError) as problem:
        say("%s" % problem)
        return 2
    except KeyboardInterrupt:
        return 130
