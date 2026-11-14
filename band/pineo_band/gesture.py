"""Wrist-rotation gestures from gyro samples. Pure: no band, no clock, no I/O.

Words (the same as gesture/analyze.py):
  peak      a stretch of the chosen signal on one side of zero that reaches `threshold_dps`.
            Its duration is the time it spends above HALF the threshold and must lie in
            [min_peak_ms, max_peak_ms]: shorter is a knock, longer is the arm moving.
  rotation  one peak, together with at most one "turn back" peak (opposite sign; any peak
            for axis "mag") that starts within `return_gap_ms` of its end.
  group     rotations whose starts are at most `max_gap_ms` apart.

Decisions (`bug_open` is told to the detector by the caller; it also flips it itself the
moment it emits, so the next gesture is judged correctly before the hub answers):
  no bug open:  group of 2 -> "open".  1 -> nothing.  3 or more -> nothing (shaking).
  bug open:     group of 1 -> "submit". 2 -> "submit" (double_while_open = "submit", the
                default: the person clearly wants to act and nothing can be opened twice)
                or nothing ("ignore").  3 or more -> nothing.
A group is decided once nothing new has started for `decision_delay_ms` after its last
peak; a lone rotation with no bug open waits `max_gap_ms` from its start for a second one.
With a bug open and "submit" policy a single is decided after `decision_delay_ms`, so
submitting is quick. After every decision, `refractory_ms` of samples are ignored.

Parameters come from band/gesture.json so calibrated numbers drop in without code changes.
Time is the band's own counter `t` in milliseconds, never the nominal rate.
"""
import json
import math
from collections import deque
from dataclasses import dataclass, fields
from pathlib import Path

AXES = ("gx", "gy", "gz", "mag")
NEAR_MISS_DPS = 250.0        # activity reaching this with no gesture emitted is reported as a near miss
# Reasons decided for a whole group win over reasons about one lobe when a near miss is explained.
_DECISIVE = ("shaking", "below single_min_peak", "below double_min_peak", "no return", "gap between rotations", "lobe too long")
DEFAULT_FILE = Path(__file__).resolve().parents[1] / "gesture.json"


class GestureConfigError(Exception):
    """One sentence for the person at the keyboard."""


@dataclass
class GestureParams:
    axis: str = "gx"                    # gx | gy | gz | mag (magnitude of all three, unsigned)
    direction: str = "any"              # any | positive | negative: sign of a rotation's first peak
    smooth_ms: float = 25.0             # moving average over this much time; 0 = none
    threshold_dps: float = 200.0
    min_peak_ms: float = 40.0
    max_peak_ms: float = 600.0
    return_gap_ms: float = 500.0
    max_gap_ms: float = 1200.0          # between the STARTS of two rotations of a double
    decision_delay_ms: float = 350.0
    refractory_ms: float = 1500.0
    double_while_open: str = "submit"   # submit | ignore
    release_dps: float = 0.0            # a peak ends below this; 0 = half the threshold
    require_return: bool = False        # True: a rotation counts only with its turn back (a lone lobe is nothing)
    return_min_dps: float = 0.0         # >0: the turn back only has to reach this (0 = it must reach threshold_dps)
    double_min_peak_dps: float = 0.0    # >0: in a double, the weaker first lobe must reach this
    single_min_peak_dps: float = 0.0    # >0: a lone rotation counts only if its first lobe reached this (doubles: threshold)
    purity: float = 0.0                 # >0: reject a peak when the other two gyro axes exceed purity x its size

    def validate(self) -> "GestureParams":
        if self.axis not in AXES:
            raise GestureConfigError("gesture axis must be one of %s." % ", ".join(AXES))
        if self.direction not in ("any", "positive", "negative"):
            raise GestureConfigError('gesture direction must be "any", "positive" or "negative".')
        if self.double_while_open not in ("submit", "ignore"):
            raise GestureConfigError('double_while_open must be "submit" or "ignore".')
        for name in ("smooth_ms", "threshold_dps", "min_peak_ms", "max_peak_ms", "return_gap_ms", "max_gap_ms",
                     "decision_delay_ms", "refractory_ms", "release_dps", "purity", "single_min_peak_dps", "double_min_peak_dps", "return_min_dps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise GestureConfigError("gesture %s must be a number, 0 or more." % name)
        if self.threshold_dps <= 0:
            raise GestureConfigError("gesture threshold_dps must be above 0.")
        if self.max_peak_ms < self.min_peak_ms:
            raise GestureConfigError("gesture max_peak_ms must not be below min_peak_ms.")
        return self


def load_params(path=None) -> GestureParams:
    """band/gesture.json -> GestureParams. Unknown keys (and "_comment") are ignored; a
    missing file gives the provisional defaults."""
    path = Path(path) if path else DEFAULT_FILE
    if not path.exists():
        return GestureParams().validate()
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as problem:
        raise GestureConfigError("Could not read %s: %s" % (path, problem)) from None
    if not isinstance(found, dict):
        raise GestureConfigError("%s must hold one JSON object." % path)
    names = {f.name for f in fields(GestureParams)}
    return GestureParams(**{k: v for k, v in found.items() if k in names}).validate()


@dataclass
class GestureEvent:
    name: str            # "open" | "submit"
    t_ms: float          # band time of the decision
    rotations: int       # how many rotations the group had
    peak_dps: float      # largest turning speed seen in the group
    confidence: float    # 0..1: how far above the threshold the weakest rotation was
    detail: str = ""     # per rotation: first lobe peak / duration / other axes, and the turn-back peak


class GestureDetector:
    def __init__(self, params: GestureParams = None, bug_open: bool = False) -> None:
        self.p = (params or GestureParams()).validate()
        self.bug_open = bug_open
        self.log = []                    # (t_ms, text): why things were or were not counted; for --replay
        self.keep_log = False
        self.rejections = []             # near misses: (t_ms, reason, lobe list as text); drained by the caller
        self._why = []                   # (code, text) of what was refused since the last near-miss report
        self._sh_lobe, self._sh_lobes, self._sh_last = None, [], 0.0     # a second, passive lobe tracker, for reporting only
        self.raw = False                 # True: emit "single"/"double" for every group, ignoring bug_open (offline checks)
        self._reset(None)

    # ---- state

    def _reset(self, quiet_until) -> None:
        self._window = deque()           # (t, value) for smoothing
        self._sum = 0.0
        self._lobe = None                # dict(start, sign, peak, reached, too_long)
        self._wrong_way = None           # a wrong-direction peak whose turn back must not count either
        self._group = []                 # rotations: dict(start, end, peak, returned)
        self._last_activity = None       # end of the last peak that belonged to the group
        self._quiet_until = quiet_until
        self._last_t = None

    def set_bug_open(self, is_open: bool) -> None:
        self.bug_open = bool(is_open)

    def _note(self, t, text) -> None:
        if self.keep_log:
            self.log.append((t, text))

    def _refuse(self, t, code, text) -> None:
        """Something was not counted: remembered (for the near-miss line) and noted (for --replay -v)."""
        self._why.append((code, text))
        del self._why[:-12]
        self._note(t, text)

    def take_rejections(self) -> list:
        found, self.rejections = self.rejections, []
        return found

    # ---- near misses: reporting only, never changes what is detected

    def _shadow(self, t, value, quiet) -> None:
        half = self.p.release_dps or self.p.threshold_dps / 2.0
        sign = 1 if value > 0 else -1
        lobe = self._sh_lobe
        if lobe is not None and (abs(value) < half or sign != lobe["sign"]):
            if lobe["peak"] >= NEAR_MISS_DPS:
                self._sh_lobes.append(lobe)
                self._sh_last = lobe["last"]
            self._sh_lobe = lobe = None
        if lobe is None and abs(value) >= half:
            self._sh_lobe = lobe = dict(start=t, last=t, sign=sign, peak=0.0, other=0.0, quiet=quiet)
        if lobe is not None:
            lobe["last"] = t
            lobe["peak"] = max(lobe["peak"], abs(value))
            lobe["other"] = max(lobe["other"], self._other)

    def _settle(self, t, emitted) -> None:
        if emitted:
            self._sh_lobes, self._why = [], []
            return
        if not self._sh_lobes or self._sh_lobe is not None or self._group:
            return
        if t - self._sh_last < max(self.p.decision_delay_ms, 400.0):
            return
        lobes, self._sh_lobes = self._sh_lobes, []
        why, self._why = self._why, []
        first = lobes[0]["start"]
        top = max(lobe["peak"] for lobe in lobes)
        if lobes[0]["quiet"]:                             # it began while samples were being ignored
            reason = "refractory: it began within %g ms after the last decision" % self.p.refractory_ms
        else:
            decisive = [text for code, text in why if code in _DECISIVE]
            reason = decisive[-1] if decisive else why[0][1] if why else None      # else the first thing that went wrong
            if reason is None or (why and why[-1][0] == "no return" and not decisive[:-1]):
                weak = [lobe for lobe in lobes[1:] if lobe["sign"] != lobes[0]["sign"] and lobe["peak"] < self.p.threshold_dps]
                if top < self.p.threshold_dps:
                    reason = "below threshold: largest lobe %.0f dps < threshold_dps %g" % (top, self.p.threshold_dps)
                elif weak and reason is not None:
                    reason = "return lobe too weak: %.0f dps < threshold_dps %g" % (weak[0]["peak"], self.p.threshold_dps)
                elif reason is None:
                    reason = "no gesture"
        detail = "; ".join("%+.0f dps %.0f ms other %.0f @%.0f ms" % (lobe["sign"] * lobe["peak"], lobe["last"] - lobe["start"],
                                                                      lobe["other"], lobe["start"] - first) for lobe in lobes[:8])
        self.rejections.append((t, reason, detail))
        del self.rejections[:-50]
        self._note(t, "REJECTED (%s) [%s]" % (reason, detail))

    # ---- input

    def feed(self, t_ms: float, gx: float, gy: float, gz: float) -> list:
        """One gyro sample. Returns the events decided at this sample (usually none)."""
        if gx is None or gy is None or gz is None:
            return []
        if self._last_t is not None and (t_ms < self._last_t or t_ms - self._last_t > 2000):
            self._reset(None)            # a new connection or a hole in the stream: start clean
        self._last_t = t_ms
        raw_value = self._pick(gx, gy, gz)
        self._other = 0.0 if self.p.axis == "mag" else max(abs(v) for k, v in (("gx", gx), ("gy", gy), ("gz", gz)) if k != self.p.axis)
        if self._quiet_until is not None:
            if t_ms < self._quiet_until:
                self._shadow(t_ms, raw_value, True)
                self._settle(t_ms, False)
                return []
            self._quiet_until = None
        value = self._smooth(t_ms, raw_value)
        self._shadow(t_ms, value, False)
        self._track(t_ms, value)
        events = self._decide(t_ms)
        self._settle(t_ms, bool(events))
        return events

    def _pick(self, gx, gy, gz) -> float:
        if self.p.axis == "mag":
            return math.sqrt(gx * gx + gy * gy + gz * gz)
        return {"gx": gx, "gy": gy, "gz": gz}[self.p.axis]

    def _smooth(self, t, value) -> float:
        if self.p.smooth_ms <= 0:
            return value
        self._window.append((t, value))
        self._sum += value
        while self._window and t - self._window[0][0] > self.p.smooth_ms:
            self._sum -= self._window.popleft()[1]
        return self._sum / len(self._window)

    # ---- peaks

    def _track(self, t, value) -> None:
        half = self.p.release_dps or self.p.threshold_dps / 2.0
        sign = 1 if value > 0 else -1
        lobe = self._lobe
        if lobe is not None and (abs(value) < half or sign != lobe["sign"]):
            self._end_lobe(lobe)
            self._lobe = lobe = None
        if lobe is None and abs(value) >= half:
            self._lobe = lobe = dict(start=t, last=t, sign=sign, peak=0.0, reached=False, too_long=False, other=0.0)
        if lobe is not None:
            lobe["last"] = t
            lobe["peak"] = max(lobe["peak"], abs(value))
            lobe["other"] = max(lobe["other"], self._other)
            if lobe["peak"] >= self.p.threshold_dps:
                lobe["reached"] = True
            if not lobe["too_long"] and t - lobe["start"] > self.p.max_peak_ms:
                lobe["too_long"] = True
                if lobe["reached"]:
                    self._refuse(t, "lobe too long", "lobe too long: over max_peak_ms %g%s" % (
                        self.p.max_peak_ms, ", pending rotations dropped" if self._group else ""))
                if lobe["reached"] and self._group:
                    self._group, self._last_activity = [], None

    def _end_lobe(self, lobe) -> None:
        duration = lobe["last"] - lobe["start"]
        last = self._group[-1] if self._group else None
        unsigned = self.p.axis == "mag"
        if not lobe["reached"] and not lobe["too_long"] and self.p.return_min_dps > 0 and lobe["peak"] >= self.p.return_min_dps \
                and last and not last["returned"] and (unsigned or lobe["sign"] != last["sign"]) \
                and lobe["start"] - last["end"] <= self.p.return_gap_ms:
            last["returned"] = True                       # a gentle turn back still counts (return_min_dps)
            last["back"], last["back_ms"] = lobe["peak"], duration
            self._last_activity = lobe["last"]
            return
        if not lobe["reached"] or lobe["too_long"]:
            return
        if duration < self.p.min_peak_ms:
            self._refuse(lobe["last"], "lobe too short", "lobe too short: %.0f dps lasted %.0f ms < min_peak_ms %g" % (lobe["peak"], duration, self.p.min_peak_ms))
            return
        if self.p.purity > 0 and lobe["other"] > self.p.purity * lobe["peak"]:
            self._refuse(lobe["last"], "purity", "purity fail: other axes %.0f / lobe %.0f = %.2f > purity %g" % (
                lobe["other"], lobe["peak"], lobe["other"] / lobe["peak"], self.p.purity))
            return
        if last and not last["returned"] and (unsigned or lobe["sign"] != last["sign"]) \
                and lobe["start"] - last["end"] <= self.p.return_gap_ms:
            last["returned"] = True                       # the wrist turning back
            last["back"], last["back_ms"] = lobe["peak"], duration
            self._last_activity = lobe["last"]
            return
        if not unsigned and self.p.direction != "any" and (lobe["sign"] > 0) != (self.p.direction == "positive"):
            back = self._wrong_way
            if back and lobe["sign"] != back["sign"] and lobe["start"] - back["end"] <= self.p.return_gap_ms:
                self._wrong_way = None                    # only the turn back of a rotation that did count the other way
                return
            self._refuse(lobe["last"], "wrong direction", "first lobe %s: %+.0f dps, but direction is %s" % (
                "negative" if lobe["sign"] < 0 else "positive", lobe["sign"] * lobe["peak"], self.p.direction))
            self._wrong_way = dict(end=lobe["last"], sign=lobe["sign"])
            return
        back = self._wrong_way
        self._wrong_way = None
        if back and not unsigned and lobe["sign"] != back["sign"] and lobe["start"] - back["end"] <= self.p.return_gap_ms:
            self._note(lobe["last"], "peak ignored: it is the turn back of a wrong-direction rotation")
            return
        if self._group and lobe["start"] - self._group[-1]["start"] > self.p.max_gap_ms:
            self._refuse(lobe["last"], "gap between rotations", "gap between rotations too long: %.0f ms > max_gap_ms %g (the earlier one was forgotten)"
                         % (lobe["start"] - self._group[-1]["start"], self.p.max_gap_ms))
            self._group = []
        self._group.append(dict(start=lobe["start"], end=lobe["last"], sign=lobe["sign"], peak=lobe["peak"], returned=False,
                                ms=duration, other=lobe["other"], back=0.0, back_ms=0.0))
        self._last_activity = lobe["last"]
        self._note(lobe["last"], "rotation %d: %+.0f deg/s, %.0f ms" % (len(self._group), lobe["sign"] * lobe["peak"], duration))

    # ---- decisions

    def _decide(self, t) -> list:
        if not self._group:
            return []
        lobe = self._lobe
        if lobe is not None and lobe["reached"] and not lobe["too_long"]:
            return []                                     # something is still happening: wait for it to end
        counted = [r for r in self._group if r["returned"]] if self.p.require_return else self._group
        count = len(counted)
        fast_submit = self.bug_open and self.p.double_while_open == "submit" and not self.raw
        quiet_for = t - self._last_activity
        if len(self._group) == 1 and not fast_submit:
            # a second rotation may still come: its START must be within max_gap of the first's start
            if t - self._group[0]["start"] <= self.p.max_gap_ms or quiet_for < self.p.decision_delay_ms:
                return []
        elif quiet_for < self.p.decision_delay_ms:
            return []
        name = None
        if self.raw:                                      # --replay --raw: what was seen, whatever is open
            name = {1: "single", 2: "double"}.get(count)
        elif count == 1:
            name = "submit" if self.bug_open else None
        elif count == 2:
            if not self.bug_open:
                name = "open"
            elif self.p.double_while_open == "submit":
                name = "submit"
        if len(self._group) >= 3 and count < 3:           # three lobes are shaking even when one had no turn back
            self._refuse(t, "shaking", "3+ rotations treated as shaking: %d in a row" % len(self._group))
            self._reset(t + self.p.refractory_ms)
            self._last_t = t
            return []
        if count == 2 and self.p.double_min_peak_dps > 0 and min(r["peak"] for r in counted) < self.p.double_min_peak_dps:
            self._refuse(t, "below double_min_peak", "below double_min_peak: a rotation of %.0f dps < double_min_peak_dps %g"
                         % (min(r["peak"] for r in counted), self.p.double_min_peak_dps))
            self._reset(t + self.p.refractory_ms)
            self._last_t = t
            return []
        if count == 1 and self.p.single_min_peak_dps > 0 and counted[0]["peak"] < self.p.single_min_peak_dps:
            self._refuse(t, "below single_min_peak", "below single_min_peak: %.0f dps < single_min_peak_dps %g" % (
                counted[0]["peak"], self.p.single_min_peak_dps))
            self._reset(None)
            self._last_t = t
            return []
        peak = max(r["peak"] for r in self._group)
        weakest = min(r["peak"] for r in self._group)
        detail = "; ".join("%+.0f dps %.0f ms other %.0f, back %.0f dps %.0f ms" % (r["sign"] * r["peak"], r["ms"], r["other"], r["back"], r["back_ms"])
                           for r in self._group)
        if name is None:
            if count == 0:
                self._refuse(t, "no return", "no return within return_gap_ms %g: the rotation had no turn back" % self.p.return_gap_ms)
            elif count >= 3:
                self._refuse(t, "shaking", "3+ rotations treated as shaking: %d in a row" % count)
            else:
                self._note(t, "%d rotation%s, nothing sent: %s" % (count, "" if count == 1 else "s",
                                                                   "no bug is open" if count == 1 else "a bug is already open"))
            # a lone rotation with nothing open costs nothing: no refractory, so a double right after still works
            self._reset(t + self.p.refractory_ms if count > 1 else None)   # count 0/1: a double right after still works
            self._last_t = t
            return []
        confidence = max(0.0, min(1.0, (weakest / self.p.threshold_dps - 1.0) / 1.0 * 0.5 + 0.5))
        event = GestureEvent(name, t, count, round(peak, 1), round(confidence, 2), detail)
        if not self.raw:
            self.bug_open = name == "open"
        self._note(t, "%s (%d rotation%s)" % (name.upper(), count, "" if count == 1 else "s"))
        self._reset(t + self.p.refractory_ms)
        self._last_t = t
        return [event]


def replay(path, params: GestureParams = None, bug_open: bool = False, verbose: bool = False, raw: bool = False):
    """Run the detector over a samples.csv made by gesture/record.py (columns t_ms, gx, gy,
    gz; `phase` is shown when present). Returns (events, lines to print)."""
    import csv
    detector = GestureDetector(params, bug_open)
    detector.keep_log = True
    detector.raw = raw
    events, lines, first, count, shown = [], [], None, 0, 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                t = float(row["t_ms"])
                g = [float(row[k]) for k in ("gx", "gy", "gz")]
            except (KeyError, ValueError, TypeError):
                continue                                  # accel-only rows or a broken line
            first = t if first is None else first
            count += 1
            for event in detector.feed(t, *g):
                events.append(event)
                lines.append("%8.2f s  %-6s  rotations=%d  peak=%.0f deg/s  confidence=%.2f  phase=%s%s"
                             % ((event.t_ms - first) / 1000.0, event.name.upper(), event.rotations, event.peak_dps,
                                event.confidence, row.get("phase", "?"), ("  [%s]" % event.detail) if verbose else ""))
            for at, text in detector.log[shown:]:
                if text.startswith("REJECTED") or (verbose and not text.startswith(("OPEN", "SUBMIT", "SINGLE", "DOUBLE"))):
                    lines.append("%8.2f s    %s %s" % ((at - first) / 1000.0, "x" if text.startswith("REJECTED") else ".", text))
            shown = len(detector.log)
    tally = {}
    for event in events:
        tally[event.name] = tally.get(event.name, 0) + 1
    lines.append("%d samples, %d event%s: %s." % (count, len(events), "" if len(events) == 1 else "s",
                                                  ", ".join("%d %s" % (tally.get(n, 0), n) for n in
                                                            (("double", "single") if raw else ("open", "submit")))))
    return events, lines
