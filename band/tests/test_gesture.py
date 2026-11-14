import json
import math

import pytest

from pineo_band.gesture import GestureConfigError, GestureDetector, GestureParams, load_params, replay

RATE = 50.0


def twist(amp=400.0, ms=160.0):
    """One wrist rotation on gx: out (+) and straight back (-), each a half sine."""
    n = int(ms / 1000.0 * RATE)
    out = [amp * math.sin(math.pi * (i + 0.5) / n) for i in range(n)]
    return out + [-v for v in out]


def rest(ms):
    return [0.0] * int(ms / 1000.0 * RATE)


def run(signal, bug_open=False, params=None, axis=0, detector=None):
    detector = detector or GestureDetector(params, bug_open)
    events = []
    for i, value in enumerate(signal):
        g = [1.5, -2.0, 0.7]
        g[axis] = value
        events += detector.feed(i * 1000.0 / RATE, *g)
    return events, detector


def names(events):
    return [e.name for e in events]


def test_double_opens_when_nothing_is_open():
    events, detector = run(rest(500) + twist() + rest(300) + twist() + rest(2500))
    assert names(events) == ["open"]
    assert events[0].rotations == 2 and events[0].peak_dps > 300 and 0.5 < events[0].confidence <= 1
    assert detector.bug_open is True


def test_single_does_nothing_when_nothing_is_open():
    events, detector = run(rest(500) + twist() + rest(4000))
    assert events == [] and detector.bug_open is False


def test_single_submits_when_a_bug_is_open_and_quickly():
    signal = rest(500) + twist() + rest(3000)
    events, detector = run(signal, bug_open=True)
    assert names(events) == ["submit"] and events[0].rotations == 1
    assert detector.bug_open is False
    assert events[0].t_ms < 500 + 320 + 450            # twist end + decision delay + a sample or two


def test_double_while_open_is_one_submit_by_default():
    events, _ = run(rest(500) + twist() + rest(300) + twist() + rest(3000), bug_open=True)
    assert names(events) == ["submit"]


def test_double_while_open_can_be_ignored():
    params = GestureParams(double_while_open="ignore")
    events, detector = run(rest(500) + twist() + rest(300) + twist() + rest(3000), bug_open=True, params=params)
    assert events == [] and detector.bug_open is True
    events, _ = run(rest(500) + twist() + rest(3000), bug_open=True, params=params)
    assert names(events) == ["submit"]


def test_open_then_submit_in_one_stream():
    signal = rest(500) + twist() + rest(300) + twist() + rest(4000) + twist() + rest(3000)
    events, _ = run(signal)
    assert names(events) == ["open", "submit"]


def test_double_with_too_long_a_gap_is_two_singles():
    signal = rest(500) + twist() + rest(1800) + twist() + rest(3000)
    assert run(signal)[0] == []
    assert names(run(signal, bug_open=True)[0]) == ["submit"]     # the 2nd finds nothing open any more


def test_slow_drift_is_ignored():
    # the arm turning slowly: big, but far longer than a wrist flick
    slow = [300.0 * math.sin(math.pi * i / 100.0) for i in range(100)]          # one 2 s lobe
    assert run(rest(300) + slow + rest(3000))[0] == []
    assert run(rest(300) + slow + rest(3000), bug_open=True)[0] == []
    gentle = [60.0 * math.sin(2 * math.pi * i / 50.0) for i in range(300)]      # below the threshold
    assert run(gentle, bug_open=True)[0] == []


def test_shaking_is_ignored():
    shake = [400.0 * math.sin(2 * math.pi * 4.0 * i / RATE) for i in range(int(RATE * 3))]   # 4 Hz for 3 s
    assert run(rest(300) + shake + rest(3000))[0] == []
    assert run(rest(300) + shake + rest(3000), bug_open=True)[0] == []
    buzz = [500.0 * (1 if i % 2 else -1) for i in range(150)]                   # 25 Hz: every peak too short
    assert run(buzz + rest(2000), bug_open=True, params=GestureParams(smooth_ms=0))[0] == []


def test_refractory_swallows_what_follows_a_decision():
    signal = rest(300) + twist() + rest(300) + twist() + rest(450) + twist() + rest(3000)
    events, _ = run(signal)
    assert names(events) == ["open"]                    # the third twist lands in the refractory time


def test_other_axes_and_magnitude():
    signal = rest(500) + twist() + rest(300) + twist() + rest(2500)
    assert run(signal, axis=2)[0] == []                                          # motion on gz, watching gx
    assert names(run(signal, axis=2, params=GestureParams(axis="gz"))[0]) == ["open"]
    assert names(run(signal, axis=1, params=GestureParams(axis="mag"))[0]) == ["open"]


def test_direction_filter():
    signal = rest(500) + twist() + rest(300) + twist() + rest(2500)
    assert names(run(signal, params=GestureParams(direction="positive"))[0]) == ["open"]
    assert run(signal, params=GestureParams(direction="negative"))[0] == []
    flipped = [-v for v in signal]
    assert names(run(flipped, params=GestureParams(direction="negative"))[0]) == ["open"]


def test_hub_state_overrides_and_time_reset():
    detector = GestureDetector(bug_open=False)
    detector.set_bug_open(True)
    events, _ = run(rest(300) + twist() + rest(2000), detector=detector)
    assert names(events) == ["submit"]
    # the band's clock starts again (a new connection): no crash, clean state
    assert detector.feed(5.0, 0.0, 0.0, 0.0) == []
    assert detector.feed(10.0, None, None, None) == []   # accel-only sample


def test_params_file(tmp_path):
    path = tmp_path / "gesture.json"
    path.write_text(json.dumps({"_comment": "x", "axis": "gz", "threshold_dps": 150, "unknown": 1}))
    params = load_params(path)
    assert params.axis == "gz" and params.threshold_dps == 150 and params.max_gap_ms == 1200
    assert load_params(tmp_path / "missing.json").axis == "gx"
    path.write_text(json.dumps({"axis": "qq"}))
    with pytest.raises(GestureConfigError):
        load_params(path)
    path.write_text("{nope")
    with pytest.raises(GestureConfigError):
        load_params(path)
    assert load_params().threshold_dps > 0               # the real band/gesture.json parses


def test_replay_reads_the_recorder_format(tmp_path):
    path = tmp_path / "samples.csv"
    signal = rest(500) + twist() + rest(300) + twist() + rest(2500)
    rows = ["mono_s,time_s,t_ms,n,ax,ay,az,gx,gy,gz,phase"]
    for i, value in enumerate(signal):
        rows.append("1.0,2.0,%.3f,%d,0.0,0.1,1.0,%.1f,0.2,0.1,twice" % (i * 20.0, i, value))
    rows.append("1.0,2.0,99999,1,0.0,0.1,1.0,None,None,None,rest")
    path.write_text("\n".join(rows) + "\n")
    events, lines = replay(path, verbose=True)
    assert names(events) == ["open"]
    assert any("OPEN" in line and "phase=twice" in line for line in lines) and "1 open, 0 submit" in lines[-1]
    extra = rows[:-1] + ["1.0,2.0,%.3f,%d,0.0,0.1,1.0,%.1f,0.2,0.1,once" % (10000 + i * 20.0, i, v)
                         for i, v in enumerate(twist() + rest(2500))]
    path.write_text("\n".join(extra) + "\n")
    events, lines = replay(path, raw=True)                 # every group, whatever is open
    assert names(events) == ["double", "single"] and "1 double, 1 single" in lines[-1]


def test_raw_mode_reports_every_single_and_double_whatever_is_open():
    signal = rest(300) + twist() + rest(300) + twist() + rest(3000) + twist() + rest(3000) + twist() + rest(300) + twist() + rest(3000)
    for is_open in (False, True):
        detector = GestureDetector(bug_open=is_open)
        detector.raw = True
        events, _ = run(signal, detector=detector)
        assert names(events) == ["double", "single", "double"] and detector.bug_open is is_open
    shake = [400.0 * math.sin(2 * math.pi * 4.0 * i / RATE) for i in range(int(RATE * 3))]
    detector = GestureDetector()
    detector.raw = True
    assert run(rest(300) + shake + rest(3000), detector=detector)[0] == []        # 3+ rotations is still shaking
    detector = GestureDetector()
    detector.raw = True
    assert names(run(rest(300) + twist() + rest(300) + twist() + rest(450) + twist() + rest(3000), detector=detector)[0]) == ["double"]  # refractory


def test_weak_singles_can_be_refused_while_doubles_keep_the_threshold():
    params = GestureParams(single_min_peak_dps=700, require_return=True)
    weak = rest(300) + twist(amp=450) + rest(3000)
    for is_open in (False, True):
        detector = GestureDetector(params, bug_open=is_open)
        detector.raw = True
        assert run(weak, detector=detector)[0] == []
        assert names(run(rest(300) + twist(amp=450) + rest(300) + twist(amp=450) + rest(3000), detector=GestureDetector(params))[0]) == ["open"]
    detector = GestureDetector(params)
    detector.raw = True
    events, _ = run(rest(300) + twist(amp=1000) + rest(3000), detector=detector)
    assert names(events) == ["single"]
    assert "dps" in events[0].detail and "back" in events[0].detail and "other" in events[0].detail


def test_three_lobes_are_shaking_even_if_one_has_no_turn_back_and_weak_doubles_are_refused():
    half = [400.0 * math.sin(math.pi * (i + 0.5) / 8) for i in range(8)]            # one lobe, no turn back
    params = GestureParams(require_return=True)
    detector = GestureDetector(params)
    detector.raw = True
    assert run(rest(300) + twist() + rest(150) + twist() + rest(150) + half + rest(3000), detector=detector)[0] == []
    params = GestureParams(require_return=True, double_min_peak_dps=500)
    detector = GestureDetector(params)
    detector.raw = True
    assert run(rest(300) + twist(amp=420) + rest(300) + twist(amp=900) + rest(3000), detector=detector)[0] == []
    detector = GestureDetector(params)
    detector.raw = True
    assert names(run(rest(300) + twist(amp=700) + rest(300) + twist(amp=900) + rest(3000), detector=detector)[0]) == ["double"]


def lobe(amp, ms, sign=1):
    n = max(1, int(ms / 1000.0 * RATE))
    return [sign * amp * math.sin(math.pi * (i + 0.5) / n) for i in range(n)]


def near_misses(signal, params, other=0.0, raw=True, prefix=None):
    detector = GestureDetector(params)
    detector.raw = raw
    events = []
    for i, value in enumerate((prefix or []) + signal):
        events += detector.feed(i * 1000.0 / RATE, value, other if abs(value) > 100 else 0.0, 0.0)
    return events, detector.take_rejections()


CAL = dict(axis="gx", direction="positive", smooth_ms=0, threshold_dps=350, release_dps=150, min_peak_ms=60, max_peak_ms=350,
           return_gap_ms=350, require_return=True, purity=0.5, max_gap_ms=900, decision_delay_ms=400, refractory_ms=800,
           single_min_peak_dps=700, double_min_peak_dps=500)


def test_every_near_miss_is_reported_once_with_its_reason():
    params = GestureParams(**CAL)
    cases = {
        "below single_min_peak": rest(300) + twist(amp=600) + rest(2500),
        "below double_min_peak": rest(300) + twist(amp=450) + rest(200) + twist(amp=900) + rest(2500),
        "below threshold": rest(300) + twist(amp=300) + rest(2500),
        "lobe too short": rest(300) + lobe(900, 40) + lobe(900, 160, -1) + rest(2500),
        "lobe too long": rest(300) + lobe(900, 500) + rest(2500),
        "no return within return_gap": rest(300) + lobe(900, 160) + rest(2500),
        "return lobe too weak": rest(300) + lobe(900, 160) + lobe(300, 160, -1) + rest(2500),
        "first lobe negative": rest(300) + lobe(900, 160, -1) + rest(2500),
        "3+ rotations treated as shaking": rest(300) + twist(amp=900) + rest(100) + twist(amp=900) + rest(100) + twist(amp=900) + rest(2500),
    }
    for reason, signal in cases.items():
        events, missed = near_misses(signal, params)
        assert events == [], reason
        assert len(missed) == 1 and reason in missed[0][1], (reason, missed)
        assert "dps" in missed[0][2] and "other" in missed[0][2] and "@0 ms" in missed[0][2]
    events, missed = near_misses(rest(300) + twist(amp=900) + rest(2500), params, other=600.0)
    assert events == [] and "purity fail" in missed[0][1] and "0.6" in missed[0][1]
    # swallowed by the refractory time after a gesture
    events, missed = near_misses(rest(300) + twist(amp=900) + rest(1300) + twist(amp=900) + rest(2500), params)
    assert names(events) == ["single"] and len(missed) == 1 and missed[0][1].startswith("refractory")
    # an accepted gesture and plain rest report nothing
    assert near_misses(rest(300) + twist(amp=900) + rest(300) + twist(amp=900) + rest(2500), params) == \
        (near_misses(rest(300) + twist(amp=900) + rest(300) + twist(amp=900) + rest(2500), params)[0], [])
    assert near_misses(rest(3000) + [120.0] * 20 + rest(2000), params)[1] == []


def test_a_gentle_turn_back_can_be_allowed():
    signal = rest(300) + lobe(900, 160) + lobe(250, 160, -1) + rest(2500)
    assert near_misses(signal, GestureParams(**CAL))[0] == []
    assert names(near_misses(signal, GestureParams(**dict(CAL, return_min_dps=200)))[0]) == ["single"]
