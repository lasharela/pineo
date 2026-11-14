"""Read one gesture recording and say how to detect the two gestures.

    .venv/bin/python gesture/analyze.py                      the newest recording
    .venv/bin/python gesture/analyze.py gesture/recordings/<timestamp>

Prints, for each phase, the gyroscope's numbers; then, for a few candidate thresholds, the
turns found in every window and how often normal use would trigger; then one recommended
detector with its hits and false alarms on this recording. Saves plot.png next to the data.

Words used here:
  lobe      a stretch of the chosen gyro axis between two zero crossings
  peak      a lobe whose largest turning speed is at least the threshold
  rotation  one peak, together with the opposite peak that follows it within 0.5 s
            (the wrist turning back)
  group     rotations whose starts are at most `max gap` apart: 2 = "open", 1 = "submit"
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
THRESHOLDS = [80, 120, 160, 200, 250, 300, 400]        # degrees/second
RETURN_GAP = 0.5                                       # s: the turn back belongs to the same rotation
SMOOTH = 0.025                                         # s: moving average against sensor noise
AXES = ["gx", "gy", "gz"]


def load(folder: Path):
    with open(folder / "samples.csv", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["gx"] not in ("", "None")]
    if len(rows) < 50:
        sys.exit("Too few samples in %s." % folder)
    data = {name: np.array([float(r[name]) for r in rows]) for name in
            ["time_s", "t_ms", "ax", "ay", "az", "gx", "gy", "gz"]}
    data["s"] = (data["t_ms"] - data["t_ms"][0]) / 1000.0            # seconds on the band's own clock
    with open(folder / "phases.csv", newline="") as f:
        phases = list(csv.DictReader(f))
    offset = data["time_s"][0]                                       # the phases' clock -> seconds of the recording
    windows = [dict(phase=p["phase"], rep=int(p["rep"]), start=float(p["start_epoch_s"]) - offset,
                    end=float(p["end_epoch_s"]) - offset) for p in phases]
    meta = json.loads((folder / "meta.json").read_text()) if (folder / "meta.json").exists() else {}
    return data, windows, meta


def smooth(values, s):
    width = max(1, int(round(SMOOTH / max(np.median(np.diff(s)), 1e-4))))
    return np.convolve(values, np.ones(width) / width, mode="same")


def inside(s, window, after=0.0):
    return (s >= window["start"]) & (s < window["end"] + after)


def lobes(signal, s):
    """[(start, end, peak value with its sign, time of the peak)] for every stretch between zero crossings."""
    sign = np.sign(signal)
    edges = np.flatnonzero(np.diff(sign) != 0) + 1
    found = []
    for a, b in zip(np.r_[0, edges], np.r_[edges, len(signal)]):
        if b - a < 2 or sign[a] == 0:
            continue
        at = a + int(np.argmax(np.abs(signal[a:b])))
        found.append((s[a], s[b - 1], float(signal[at]), s[at]))
    return found


def rotations(all_lobes, signal, s, threshold, min_dur=0.0, max_dur=9.0):
    """Peaks above the threshold, each joined with the turn back that follows it."""
    peaks = []
    for start, end, value, at in all_lobes:
        if abs(value) < threshold:
            continue
        part = (s >= start) & (s <= end) & (np.abs(signal) >= threshold / 2)
        over = s[part]
        duration = float(over[-1] - over[0]) if len(over) else 0.0   # time above half the threshold
        if min_dur <= duration <= max_dur:
            peaks.append(dict(start=float(over[0]) if len(over) else start, end=float(over[-1]) if len(over) else end,
                              value=value, at=at, duration=duration))
    found = []
    for peak in peaks:
        last = found[-1] if found else None
        if last and not last["returned"] and np.sign(peak["value"]) != np.sign(last["value"]) \
                and peak["start"] - last["end"] <= RETURN_GAP:
            last.update(end=peak["end"], returned=True, back=peak["value"])
        else:
            found.append(dict(start=peak["start"], end=peak["end"], value=peak["value"], at=peak["at"],
                              duration=peak["duration"], returned=False, back=None))
    return found


def groups(found, max_gap):
    made = []
    for rotation in found:
        if made and rotation["start"] - made[-1][-1]["start"] <= max_gap:
            made[-1].append(rotation)
        else:
            made.append([rotation])
    return made


def score(found, windows, max_gap):
    """Hits and false alarms of "2 rotations = open, 1 = submit" on the labelled windows."""
    made = groups(found, max_gap)

    def in_window(window, after=0.6):
        return [g for g in made if window["start"] <= g[0]["start"] < window["end"] + after]

    result = dict(twice_hit=0, twice_n=0, once_hit=0, once_n=0, false_open=0, false_submit=0, other=0, rest_false=0)
    for window in windows:
        here = in_window(window, 0.6 if window["phase"] in ("twice", "once") else 0.0)
        sizes = sorted(len(g) for g in here)
        if window["phase"] == "twice":
            result["twice_n"] += 1
            result["twice_hit"] += sizes == [2]
        elif window["phase"] == "once":
            result["once_n"] += 1
            result["once_hit"] += sizes == [1]
        elif window["phase"] == "normal":
            result["false_open"] += sizes.count(2)
            result["false_submit"] += sizes.count(1)
            result["other"] += sum(1 for n in sizes if n > 2)
        elif window["phase"] == "rest":
            # a gesture that began late in its window may end in the rest after it: not counted
            late = [g for g in here if g[0]["start"] >= window["start"] + 0.6]
            result["rest_false"] += len(late)
    return result


def numbers(values):
    return float(values.min()), float(values.max()), float(values.max() - values.min()), float(np.sqrt(np.mean(values ** 2)))


def main() -> int:
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
    else:
        made = sorted(p for p in (HERE / "recordings").glob("*") if (p / "samples.csv").exists())
        if not made:
            sys.exit("No recording yet. Run gesture/record.py first.")
        folder = made[-1]
    data, windows, meta = load(folder)
    s = data["s"]
    rate = (len(s) - 1) / (s[-1] - s[0])
    print("Recording %s%s" % (folder, "   (SIMULATED: made-up rotations)" if meta.get("simulated") else ""))
    print("%d samples, %.1f s, %.1f a second measured (asked %s, %s). Gyro in degrees/second, accel in g."
          % (len(s), s[-1] - s[0], rate, meta.get("rate_asked_hz", "?"), meta.get("transport", "?")))

    kinds = [k for k in ("rest", "twice", "once", "normal") if any(w["phase"] == k for w in windows)]
    print("\n== Gyroscope per phase (all windows of a phase together): min / max / peak-to-peak / RMS")
    rms = {}
    for kind in kinds:
        mask = np.zeros(len(s), bool)
        for window in windows:
            if window["phase"] == kind:
                mask |= inside(s, window)
        if not mask.any():
            continue
        print("%-7s %5.1f s" % (kind, mask.sum() / rate))
        for axis in AXES:
            low, high, span, root = numbers(data[axis][mask])
            rms[(kind, axis)] = root
            print("    %s  %8.1f %8.1f %8.1f %8.1f" % (axis, low, high, span, root))
        magnitude = np.sqrt(data["ax"][mask] ** 2 + data["ay"][mask] ** 2 + data["az"][mask] ** 2)
        print("    |a| %7.2f %8.2f g (min, max)" % (magnitude.min(), magnitude.max()))

    gesture_kinds = [k for k in ("twice", "once") if k in kinds]
    if not gesture_kinds:
        sys.exit("\nNo gesture windows in this recording: nothing to calibrate.")
    axis = max(AXES, key=lambda a: sum(rms.get((k, a), 0) ** 2 for k in gesture_kinds))
    print("\n== Dominant rotation axis in the gesture windows: %s" % axis)
    for a in AXES:
        print("    %s  RMS in gestures %7.1f   in normal use %7.1f   in rest %6.1f"
              % (a, np.sqrt(np.mean([rms.get((k, a), 0) ** 2 for k in gesture_kinds])),
                 rms.get(("normal", a), float("nan")), rms.get(("rest", a), float("nan"))))
    signal = smooth(data[axis], s)
    every = lobes(signal, s)

    print("\n== Each gesture window on %s: peaks above 120 deg/s (sign, size, time above half)" % axis)
    at_120 = rotations(every, signal, s, 120)
    for window in windows:
        if window["phase"] not in gesture_kinds:
            continue
        here = [r for r in at_120 if window["start"] <= r["start"] < window["end"] + 0.6]
        text = ["%+.0f%s %.2fs" % (r["value"], (" back %+.0f" % r["back"]) if r["returned"] else "", r["end"] - r["start"])
                for r in here]
        gaps = ["%.2f" % (b["start"] - a["start"]) for a, b in zip(here, here[1:])]
        print("  %-5s #%d: %d rotation(s): %s%s" % (window["phase"], window["rep"], len(here), "; ".join(text) or "-",
                                                      ("   gap between starts %s s" % ", ".join(gaps)) if gaps else ""))

    print("\n== Candidate thresholds on %s (max gap for a double tried at 0.8, 1.2, 1.6 s; best shown)" % axis)
    print("  thr   open hit  submit hit  normal use: false open / false submit / 3+   rest false   gap  peak durations (s)")
    scored = []
    for threshold in THRESHOLDS:
        found = rotations(every, signal, s, threshold)
        tried = [(score(found, windows, gap), gap) for gap in (0.8, 1.2, 1.6)]
        result, gap = max(tried, key=lambda t: t[0]["twice_hit"] + t[0]["once_hit"] - t[0]["false_open"]
                          - t[0]["false_submit"] - t[0]["other"] - t[0]["rest_false"])
        in_gestures = [r["duration"] for r in found
                       if any(w["phase"] in gesture_kinds and w["start"] <= r["start"] < w["end"] + 0.6 for w in windows)]
        durations = ("%.2f to %.2f" % (min(in_gestures), max(in_gestures))) if in_gestures else "-"
        print("  %3d    %d/%d       %d/%d                    %2d / %2d / %2d                       %2d      %.1f  %s"
              % (threshold, result["twice_hit"], result["twice_n"], result["once_hit"], result["once_n"],
                 result["false_open"], result["false_submit"], result["other"], result["rest_false"], gap, durations))
        value = (result["twice_hit"] + result["once_hit"] - result["false_open"] - result["false_submit"]
                 - result["other"] - result["rest_false"])
        scored.append((value, threshold, gap, result, found, in_gestures))

    top_value = max(entry[0] for entry in scored)
    tied = [entry for entry in scored if entry[0] == top_value]
    # Equally good: the middle one, with room below (noise, normal use) and above (a gentle turn).
    _, threshold, gap, result, found, in_gestures = tied[len(tied) // 2]
    twice_gaps = []
    for window in windows:
        if window["phase"] == "twice":
            here = [r for r in found if window["start"] <= r["start"] < window["end"] + 0.6]
            twice_gaps += [b["start"] - a["start"] for a, b in zip(here, here[1:])]
    max_gap = round(min(2.0, max(0.5, 1.3 * max(twice_gaps))), 2) if twice_gaps else gap
    min_dur = round(0.5 * min(in_gestures), 3) if in_gestures else 0.03
    max_dur = round(1.5 * max(in_gestures), 2) if in_gestures else 1.0
    final = score(rotations(every, signal, s, threshold, min_dur, max_dur), windows, max_gap)
    signs = [np.sign(r["value"]) for r in found
             if any(w["phase"] in gesture_kinds and w["start"] <= r["start"] < w["end"] + 0.6 for w in windows)]
    lead = "+" if sum(signs) > 0 else "-" if sum(signs) < 0 else "either"
    print("\n== Recommended detector")
    print("  axis                         %s, smoothed over %d ms" % (axis, SMOOTH * 1000))
    print("  threshold                    %d degrees/second (a peak is a lobe reaching it)" % threshold)
    print("  first peak's sign            %s (%d of %d rotations started that way)"
          % (lead, sum(1 for x in signs if (x > 0) == (lead == "+")) if lead != "either" else len(signs), len(signs)))
    print("  peak duration                %.3f to %.2f s above half the threshold" % (min_dur, max_dur))
    print("  the turn back                an opposite peak within %.1f s belongs to the same rotation" % RETURN_GAP)
    print("  max gap for a double         %.2f s between the starts of two rotations%s"
          % (max_gap, (" (seen: %.2f to %.2f s)" % (min(twice_gaps), max(twice_gaps))) if twice_gaps else " (no doubles seen)"))
    print("  decide                       %.2f s after a rotation with no second one: it was a single" % max_gap)
    print("  refractory                   %.2f s after a decision before the next rotation counts" % round(max_gap + 0.3, 2))
    print("  on this recording            open %d/%d, submit %d/%d; in normal use: %d false open, %d false submit, %d longer bursts; "
          "%d in rest" % (final["twice_hit"], final["twice_n"], final["once_hit"], final["once_n"], final["false_open"],
                          final["false_submit"], final["other"], final["rest_false"]))
    if final["twice_n"] + final["once_n"] < 6:
        print("  (few repetitions: treat these numbers as a first guess)")
    if final["false_open"] + final["false_submit"] + final["other"]:
        print("  Normal use still triggers: require the app to be in its listening state, or raise the threshold.")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colours = {"rest": "#dddddd", "twice": "#9be29b", "once": "#f2a3a3", "normal": "#a9c8f5"}
    figure, (top, bottom) = plt.subplots(2, 1, figsize=(16, 8), sharex=True)
    for name in ("ax", "ay", "az"):
        top.plot(s, data[name], linewidth=0.6, label=name)
    for name in AXES:
        bottom.plot(s, data[name], linewidth=0.6, label=name)
    bottom.axhline(threshold, color="black", linewidth=0.5, linestyle="--")
    bottom.axhline(-threshold, color="black", linewidth=0.5, linestyle="--")
    for plot in (top, bottom):
        for window in windows:
            plot.axvspan(window["start"], window["end"], color=colours.get(window["phase"], "#ffffff"), alpha=0.5, linewidth=0)
        plot.legend(loc="upper right", ncol=3)
    top.set_ylabel("acceleration (g)")
    bottom.set_ylabel("turning speed (degrees/second)")
    bottom.set_xlabel("seconds (band clock)   grey rest, green rotate twice, red rotate once, blue normal use; dashed: threshold on %s" % axis)
    top.set_title("%s   %.0f samples/s" % (folder.name, rate))
    figure.tight_layout()
    figure.savefig(folder / "plot.png", dpi=110)
    print("\nPlot: %s" % (folder / "plot.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
