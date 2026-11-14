"""Record the owner's wrist gestures for calibration: accelerometer and gyroscope at the
fastest rate the link carries, with spoken and LED cues. The microphone stays off.

    .venv/bin/python gesture/record.py              Bluetooth (double-tap the band when asked)
    .venv/bin/python gesture/record.py --usb        the USB cable
    .venv/bin/python gesture/record.py --sim        the kit's pretend band, with made-up rotations

The band number comes from PINEO_BAND_NUMBER (environment or pineo/.env) and nowhere else.
Output: gesture/recordings/<timestamp>/samples.csv, phases.csv, meta.json.
Then:   .venv/bin/python gesture/analyze.py         (the newest recording)

Units: ax ay az in g, gx gy gz in degrees/second, t_ms is the band's own counter in
milliseconds, time_s is the band's real time of the sample (epoch seconds), mono_s is this
computer's monotonic clock when the sample arrived (it arrives in bursts: use t_ms or time_s).
"""
import argparse
import csv
import json
import math
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "voice"))
from pineo_voice import band as band_side  # noqa: E402  (the same connect, double-tap prompt and retry)
from pineo_voice import config  # noqa: E402

LIGHT = {"rest": ("#ffffff", 0.08), "twice": ("#00ff00", 1.0), "once": ("#ff0000", 1.0), "normal": ("#0000ff", 0.6)}
NORMAL_TIPS = ["Use the phone normally. Scroll and tap.", "Type something.", "Tilt the phone to read.",
               "Lower your arm, then raise it.", "Walk a few steps."]


def arguments():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--usb", action="store_true", help="the USB cable instead of Bluetooth")
    p.add_argument("--sim", action="store_true", help="the kit's pretend band; rotations are made up")
    p.add_argument("--twice", type=int, default=6, help="repetitions of rotate-twice (default 6)")
    p.add_argument("--once", type=int, default=6, help="repetitions of rotate-once (default 6)")
    p.add_argument("--window", type=float, default=4.0, help="seconds for each repetition (default 4)")
    p.add_argument("--rest", type=float, default=2.0, help="seconds of rest between repetitions (default 2)")
    p.add_argument("--normal", type=float, default=30.0, help="seconds of normal phone use (default 30)")
    p.add_argument("--start-rest", type=float, default=5.0, help="seconds of rest at the start (default 5)")
    p.add_argument("--rate", default="fastest", help='samples a second, or "fastest" (default)')
    p.add_argument("--accel-range", type=int, default=4, help="accelerometer range in g: 2, 4, 8 or 16 (default 4)")
    p.add_argument("--no-say", action="store_true", help="no spoken prompts")
    p.add_argument("--out", default=str(HERE / "recordings"), help="folder for recordings")
    return p.parse_args()


def protocol(args):
    """[(phase, repetition, seconds, words spoken when it starts)]"""
    steps = [("rest", 0, args.start_rest, "Rest. Hold the phone still.")]
    for kind, count in (("twice", args.twice), ("once", args.once)):
        for rep in range(1, count + 1):
            steps.append((kind, rep, args.window, "Open: rotate twice, now" if kind == "twice" else "Submit: rotate once, now"))
            steps.append(("rest", rep, args.rest, "Rest"))
    if args.normal > 0:
        steps.append(("normal", 1, args.normal, None))        # the tips are spoken through the phase
        steps.append(("rest", 0, 3.0, "Rest"))
    return steps


class Recorder:
    def __init__(self, folder: Path) -> None:
        self.phase = "setup"
        self.count = 0
        self.lost = 0
        self.first = self.last = None
        self._last_n = None
        self._file = open(folder / "samples.csv", "w", newline="", buffering=1 << 16)
        self._rows = csv.writer(self._file)
        self._rows.writerow(["mono_s", "time_s", "t_ms", "n", "ax", "ay", "az", "gx", "gy", "gz", "phase"])
        self._lock = threading.Lock()

    def on_motion(self, m) -> None:                    # runs up to 833 times a second: only write
        with self._lock:
            if self._file.closed:
                return
            n = getattr(m, "n", None)
            if n is not None and self._last_n is not None and n > self._last_n + 1:
                self.lost += n - self._last_n - 1
            self._last_n = n
            self.count += 1
            self.first = self.first or (m.t, m.time)
            self.last = (m.t, m.time)
            self._rows.writerow(["%.4f" % time.monotonic(), "%.4f" % m.time, "%.3f" % m.t, "" if n is None else n,
                                 m.ax, m.ay, m.az, m.gx, m.gy, m.gz, self.phase])

    def close(self) -> None:
        with self._lock:
            self._file.close()


def main() -> int:
    args = arguments()
    config.load_env()
    try:
        number = config.band_number()
        transport = "sim" if args.sim else "serial" if args.usb else "ble"
        print("Looking for your band over %s..." % {"ble": "Bluetooth", "serial": "the USB cable", "sim": "the pretend link"}[transport], flush=True)
        began = time.monotonic()
        band, _speech = band_side.connect_patiently(number, transport)
    except config.ConfigError as problem:
        print("record: %s" % problem, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    print("Connected to %s in %.1f seconds." % (band.name, time.monotonic() - began), flush=True)

    stop = threading.Event()                           # the kit's band.wait() swallows Ctrl+C: use a flag
    for sign in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sign, lambda *_: stop.set())

    folder = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    recorder = Recorder(folder)
    phases = []
    speaking = []

    def speak(words, wait=False):
        if not words:
            return
        print("  >> %s" % words, flush=True)
        if args.no_say:
            return
        try:
            talk = subprocess.Popen(["say", words])
            speaking.append(talk)
            if wait:
                talk.wait()
        except OSError:
            pass

    def light(phase):
        colour, brightness = LIGHT[phase]
        try:
            band.set_led(colour, brightness=brightness)
        except Exception as problem:                   # the light is a courtesy; the recording goes on
            print("  (light: %s)" % problem, flush=True)

    def hold(seconds) -> bool:
        """Wait while samples arrive. False when stopped or the band is gone."""
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if stop.is_set() or not (band.connected or band.reconnecting):
                return False
            time.sleep(min(0.02, max(0.0, until - time.monotonic())))
        return True

    finished = False
    try:
        limits = band.motion_limits()
        settings = band.set_motion(accel_range=args.accel_range, gyro_range=2000)
        band.on("motion", recorder.on_motion)
        rate = "fastest" if args.rate == "fastest" else int(args.rate)
        streams = band.configure(motion=rate, sensors="both")
        asked = streams["motion"]
        meta = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "transport": band.transport_kind,
                "firmware": str(getattr(band.info, "firmware", "")), "clock": band.clock,
                "rate_asked_hz": asked, "limits": {"accel": limits.accel, "gyro": limits.gyro, "both": limits.both},
                "accel_range_g": getattr(settings, "accel_range", args.accel_range),
                "gyro_range_dps": getattr(settings, "gyro_range", 2000),
                "units": {"ax ay az": "g", "gx gy gz": "degrees/second", "t_ms": "band counter, milliseconds",
                          "time_s": "band real time, epoch seconds", "mono_s": "computer monotonic clock at arrival"},
                "protocol": {"twice": args.twice, "once": args.once, "window_s": args.window, "rest_s": args.rest,
                             "normal_s": args.normal, "start_rest_s": args.start_rest}, "simulated": bool(args.sim)}
        (folder / "meta.json").write_text(json.dumps(meta, indent=2))
        steps = protocol(args)
        total = sum(step[2] for step in steps)
        print("Recording to %s\nBoth sensors at %s samples a second (this link allows %s). The protocol takes %.0f seconds."
              % (folder, asked, limits.both, total), flush=True)
        light("rest")
        speak("Calibration. Green light: rotate your wrist twice. Red light: rotate once. White light: hold still.", wait=True)

        for phase, rep, seconds, words in steps:
            if stop.is_set():
                break
            light(phase)                               # the window starts when the light has changed
            start = (time.monotonic(), time.time())
            recorder.phase = phase
            speak(words)
            if args.sim and phase in ("twice", "once"):                # made-up rotations, to try the analysis
                turn = lambda s: dict(gx=420 * math.sin(2 * math.pi * s / 0.5), az=1.0)
                for i in range(2 if phase == "twice" else 1):
                    band.transport.after(0.8 + 0.9 * i, lambda: band.transport.play_motion(turn, seconds=0.5))
            if phase == "normal":
                done = True
                for i, tip in enumerate(NORMAL_TIPS):
                    speak(tip)
                    if not hold(seconds / len(NORMAL_TIPS)):
                        done = False
                        break
            else:
                done = hold(seconds)
            end = (time.monotonic(), time.time())
            phases.append([len(phases), phase, rep, "%.4f" % start[0], "%.4f" % end[0], "%.4f" % start[1], "%.4f" % end[1],
                           "yes" if done else "cut"])
            if not done:
                break
        else:
            finished = True
            speak("Done. Thank you.")
    finally:
        recorder.phase = "end"
        for call in (lambda: band.configure(motion=False), band.led_off, band.disconnect):
            try:
                call()
            except Exception:
                pass
        recorder.close()
        with open(folder / "phases.csv", "w", newline="") as f:
            rows = csv.writer(f)
            rows.writerow(["index", "phase", "rep", "start_mono_s", "end_mono_s", "start_epoch_s", "end_epoch_s", "complete"])
            rows.writerows(phases)
        for talk in speaking:
            if not finished and talk.poll() is None:
                talk.terminate()

    if recorder.count > 1 and recorder.last[0] > recorder.first[0]:
        seconds = (recorder.last[0] - recorder.first[0]) / 1000
        print("%s %d samples in %.1f seconds: %.1f a second measured, %d lost on the way."
              % ("Finished." if finished else "Stopped early.", recorder.count, seconds,
                 (recorder.count - 1) / seconds, recorder.lost), flush=True)
    else:
        print("No samples arrived.", flush=True)
    print("Light off, band released. Files: %s" % folder, flush=True)
    print("Next:  .venv/bin/python gesture/analyze.py %s" % folder, flush=True)
    return 0 if finished else 1


if __name__ == "__main__":
    sys.exit(main())
