import asyncio
from types import SimpleNamespace

import pytest

from pineo_band import bandlink, cli, config
from pineo_band.gesture import GestureDetector


def test_steps_keep_their_order():
    args = cli.arguments(["--no-band", "--gesture", "double", "--gesture", "single", "--say", "it is broken", "--gesture", "submit"])
    assert args.steps == [("gesture", "double"), ("gesture", "single"), ("say", "it is broken"), ("gesture", "submit")]


@pytest.mark.parametrize("argv", [["--say", "x"], ["--no-band"], ["--no-band", "--usb", "--say", "x"], ["--gesture", "open"],
                                  ["--no-band", "--gesture", "wave"]])
def test_bad_combinations_are_refused(argv):
    with pytest.raises(SystemExit):
        cli.arguments(argv)


def test_band_number_only_from_the_environment():
    with pytest.raises(config.ConfigError):
        config.band_number({})
    for bad in ("0", "1000", "abc", "4 2", "-3", "٤٢"):
        with pytest.raises(config.ConfigError):
            config.band_number({"PINEO_BAND_NUMBER": bad})
    assert config.band_number({"PINEO_BAND_NUMBER": " 17 "}) == 17


def test_settings():
    assert config.hub_url({}) == "http://127.0.0.1:8787"
    assert config.hub_url({"PINEO_HUB_URL": "http://10.0.0.5:8787/"}) == "http://10.0.0.5:8787"
    with pytest.raises(config.ConfigError):
        config.hub_url({}, given="10.0.0.5")
    assert config.motion_rate({}) == 50 and config.motion_rate({"PINEO_MOTION_RATE": "100"}) == 100
    assert config.motion_rate({}, given=208) == 208
    for bad in (60, 833, 1666):                       # not a rate, or too much next to the microphone on Bluetooth
        with pytest.raises(config.ConfigError):
            config.motion_rate({}, given=bad)
    found = config.minimax({"MINIMAX_API_KEY": "k"})
    assert found["base_url"].startswith("https://") and found["model"] and found["extra"] == {"reasoning_effort": "low"}
    assert config.minimax({"PINEO_LLM_EFFORT": "none"})["extra"] == {}
    with pytest.raises(config.ConfigError):
        config.minimax({"MINIMAX_BASE_URL": "http://evil.example"})


def test_env_file_takes_only_known_names(tmp_path):
    (tmp_path / ".env").write_text("PINEO_BAND_NUMBER=17\nexport MINIMAX_MODEL='m'\nPATH=/nope\n# c\nDEEPGRAM_API_KEY=\n")
    environ = {"MINIMAX_MODEL": "kept"}
    config.load_env(environ, [tmp_path])
    assert environ == {"MINIMAX_MODEL": "kept", "PINEO_BAND_NUMBER": "17"}


class NotFound(Exception):
    code = "not-found-nearby"


async def test_connect_retries_only_for_the_same_number_and_only_when_not_found():
    calls, lines = [], []

    async def connect(number, transport):
        calls.append((number, transport))
        if len(calls) < 3:
            raise NotFound("Could not see the band.")
        return "band"

    async def no_sleep(_):
        return None
    assert await bandlink.connect_patiently(17, "ble", lines.append, connect=connect, sleep=no_sleep) == "band"
    assert calls == [(17, "ble")] * 3 and sum("DOUBLE-TAP" in line for line in lines) == 2

    class Timeout(Exception):                          # e.g. get_info on a weak link after auto-reconnect gave up
        code = "timeout"
    flaky = []

    async def weak(number, transport):
        flaky.append(number)
        if len(flaky) == 1:
            raise Timeout("the band did not answer")
        if len(flaky) == 2:
            raise OSError("bluetooth hiccup")          # not even a kit error
        return "band"
    lines.clear()
    assert await bandlink.connect_patiently(17, "ble", lines.append, connect=weak, sleep=no_sleep) == "band"
    assert flaky == [17, 17, 17] and "connecting failed (timeout)" in lines[0] and "attempt 2" in lines[0] and "OSError" in lines[1]
    for fatal in ("wrong-band", "bad-arg", "no-bleak"):
        async def refuse(number, transport, fatal=fatal):
            raise type("E", (Exception,), {"code": fatal})("no")
        with pytest.raises(Exception) as caught:
            await bandlink.connect_patiently(17, "ble", lines.append, connect=refuse, sleep=no_sleep)
        assert caught.value.code == fatal

    class Wrong(Exception):
        code = "wrong-band"

    async def wrong(number, transport):
        raise Wrong("another band answered")
    with pytest.raises(Wrong):
        await bandlink.connect_patiently(17, "ble", lines.append, connect=wrong, sleep=no_sleep)

    async def never(number, transport):
        raise NotFound("no")
    with pytest.raises(NotFound):
        await bandlink.connect_patiently(17, "ble", lines.append, connect=never, sleep=no_sleep, tries=2)


def test_band_handlers_feed_the_detector_and_report_state():
    import math
    gestures, states, sound = [], [], []
    detector = GestureDetector(bug_open=True)
    link = bandlink.BandLink(17, "ble", 100, detector, gestures.append, sound.append, lambda **f: states.append(f), mic=True,
                             log=lambda text: None)
    link.band = SimpleNamespace(connected=True, transport_kind="ble")
    link.listening = True
    link.decoder = SimpleNamespace(feed=lambda packet: b"pcm" if packet.seq != 2 else b"")
    n = 0
    signal = [0.0] * 30 + [400 * math.sin(math.pi * (i + 0.5) / 16) for i in range(16)] + \
             [-400 * math.sin(math.pi * (i + 0.5) / 16) for i in range(16)] + [0.0] * 80
    for value in signal:                                # 100 samples a second
        link._on_motion(SimpleNamespace(t=n * 10.0, n=n if n < 50 else n + 5, gx=value, gy=0.0, gz=0.0, ax=None, ay=None, az=None))
        n += 1
    assert [e.name for e in gestures] == ["submit"] and link.lost_samples == 5
    for seq in (1, 2, 3):
        link._on_audio(SimpleNamespace(seq=seq, opus=b"x"))
    assert sound == [b"pcm", b"pcm"]
    link._on_battery(SimpleNamespace(percent=81))
    link._on_battery(SimpleNamespace(percent=81))
    assert states == [{"connected": True, "listening": True, "battery": 81, "transport": "ble", "link": "connected", "hint": None}]
    link._state(False)
    assert states[-1] == {"connected": False, "listening": False, "battery": 81, "transport": "ble", "link": "connecting", "hint": None}


def test_replay_command(tmp_path, capsys):
    path = tmp_path / "samples.csv"
    path.write_text("mono_s,time_s,t_ms,n,ax,ay,az,gx,gy,gz,phase\n" + "".join("0,0,%d,%d,0,0,1,0.0,0.0,0.0,rest\n" % (i * 10, i) for i in range(50)))
    assert cli.main(["--replay", str(path)]) == 0
    assert "50 samples, 0 events" in capsys.readouterr().out


def test_stream_dropped_is_quiet_during_start_up_and_loud_later():
    import time
    lines = []
    link = bandlink.BandLink(17, "ble", 50, GestureDetector(), None, None, lambda **f: None, log=lines.append)
    link._on_notice(SimpleNamespace(kind="stream-dropped", arg=3))
    assert lines == [] and link.dropped_notices == 0
    link.verbose = True
    link._on_notice(SimpleNamespace(kind="stream-dropped", arg=3))
    assert "start-up" in lines[-1] and "sound" in lines[-1]
    link._streams_at = time.monotonic() - 10
    link._on_notice(SimpleNamespace(kind="stream-dropped", arg=8))
    assert "WARNING" in lines[-1] and "motion" in lines[-1] and link.dropped_notices == 1
    link._on_notice(SimpleNamespace(kind="low-battery", arg=0))
    assert lines[-1] == "band: notice low-battery"


class FakeBand:
    def __init__(self):
        self.connected, self.transport_kind, self.calls, self.streams = True, "ble", [], {}
        self.fail_led = False

    async def configure(self, **streams):
        self.calls.append(("configure", streams))
        self.streams.update(streams)

    async def set_led(self, colour, **options):
        if self.fail_led:
            raise RuntimeError("denied")
        self.calls.append(("led", colour))

    async def led_off(self):
        self.calls.append(("led_off",))


async def test_the_microphone_follows_the_session_and_the_gyro_is_never_touched(monkeypatch):
    lines, states = [], []
    monkeypatch.setattr(bandlink.BandLink, "_new_decoder", lambda self: None)
    monkeypatch.setattr(asyncio, "sleep", asyncio.sleep)
    link = bandlink.BandLink(17, "ble", 50, GestureDetector(), None, lambda pcm: None, lambda **f: states.append(f), log=lines.append)
    band = link.band = FakeBand()
    await link._configure(band)                                   # connect with no session: gyro only
    assert band.calls == [("configure", {"motion": 50, "sensors": "gyro", "battery": 60})] and not link.listening
    await link.set_mic(True)                                      # session started
    assert band.calls[-1] == ("configure", {"audio": True}) and link.listening
    assert states[-1]["listening"] is True and any("MICROPHONE ON" in line for line in lines)
    await link.set_mic(True)                                      # again: nothing more is sent
    assert sum(c == ("configure", {"audio": True}) for c in band.calls) == 1
    link._on_audio(SimpleNamespace(seq=1, opus=b"x"))
    assert len(link.mic_on_ms) == 1 and any("sound is flowing" in line for line in lines)
    await link.set_mic(False)                                     # session ended: sound off, a green blink, dark
    assert ("configure", {"audio": False}) in band.calls and band.calls[-2:] == [("led", "#00ff00"), ("led_off",)]
    assert states[-1]["listening"] is False and not link.listening
    assert not any("motion" in c[1] for c in band.calls[1:] if c[0] == "configure")
    band.fail_led = True                                          # an LED refusal never breaks the loop
    await link.set_mic(True)
    await link.set_mic(False)
    assert not link.listening
    band.connected = False                                        # link down: remembered, applied after the reconnect
    await link.set_mic(True)
    assert link.want_mic and not link.listening
    band.connected = True
    await link._apply_mic()
    assert link.listening


class LinkBand(FakeBand):
    number, name = 17, "Vexo-HACK-17"

    def __init__(self):
        super().__init__()
        self.disconnected = 0

    def on(self, event, handler):
        pass

    async def disconnect(self):
        self.disconnected += 1
        self.connected = False


async def wait_for(check, seconds=3.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_manual_disconnect_releases_the_band_and_is_not_undone_by_the_retry_logic(monkeypatch):
    lines, states, attempts, bands = [], [], [], []

    async def connect(number, transport):
        attempts.append(number)
        if len(attempts) in (1, 2):                                   # not found twice, then there
            raise NotFound("Could not see the band.")
        bands.append(LinkBand())
        return bands[-1]
    monkeypatch.setattr(bandlink, "connect", connect)
    monkeypatch.setattr(bandlink, "RETRY_SECONDS", 0.02)
    monkeypatch.setattr(bandlink.BandLink, "_new_decoder", lambda self: None)
    link = bandlink.BandLink(17, "ble", 50, GestureDetector(), None, lambda pcm: None, lambda **f: states.append(dict(f)), log=lines.append)
    stop = asyncio.Event()
    task = asyncio.ensure_future(link.run(stop))
    assert await wait_for(lambda: any(s["link"] == "searching" and s["hint"] == bandlink.HINT for s in states))
    assert await wait_for(lambda: link.link == "connected")
    assert states[-1]["connected"] is True and states[-1]["hint"] is None
    await link.set_mic(True)
    assert link.listening
    link.release()                                                    # Disconnect on the dashboard
    assert await wait_for(lambda: link.link == "disconnected" and link.band is None)
    first = bands[0]
    assert first.disconnected == 1 and ("configure", {"audio": False}) in first.calls and first.calls[-1] == ("led_off",)
    assert states[-1]["connected"] is False and states[-1]["listening"] is False and states[-1]["link"] == "disconnected"
    count = len(attempts)
    await asyncio.sleep(0.3)                                          # many retry periods: nothing happens
    assert len(attempts) == count and not task.done()
    await link.set_mic(True)                                          # a session starting meanwhile breaks nothing
    link._on_motion(SimpleNamespace(t=1.0, n=1, gx=0.0, gy=0.0, gz=0.0))
    link.connect_now()                                                # Connect on the dashboard
    assert await wait_for(lambda: link.link == "connected" and link.listening)   # and the wanted microphone comes on
    assert len(bands) == 2
    link.release()
    link.release()
    assert await wait_for(lambda: link.link == "disconnected")
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_release_while_searching_stops_the_search(monkeypatch):
    attempts, states = [], []

    async def never(number, transport):
        attempts.append(number)
        raise NotFound("no")
    monkeypatch.setattr(bandlink, "connect", never)
    monkeypatch.setattr(bandlink, "RETRY_SECONDS", 0.02)
    link = bandlink.BandLink(17, "ble", 50, GestureDetector(), None, None, lambda **f: states.append(dict(f)), mic=False, log=lambda t: None)
    stop = asyncio.Event()
    task = asyncio.ensure_future(link.run(stop))
    assert await wait_for(lambda: len(attempts) >= 3)
    link.release()
    assert await wait_for(lambda: link.link == "disconnected")
    count = len(attempts)
    await asyncio.sleep(0.2)
    assert len(attempts) == count and states[-1]["hint"] is None
    link.wanted.clear()                                               # --no-autoconnect: starts idle
    stop.set()
    await asyncio.wait_for(task, 2)
    idle = bandlink.BandLink(17, "ble", 50, GestureDetector(), None, None, lambda **f: None, mic=False, log=lambda t: None)
    idle.wanted.clear()
    stop2, before = asyncio.Event(), len(attempts)
    task = asyncio.ensure_future(idle.run(stop2))
    await asyncio.sleep(0.15)
    assert len(attempts) == before and idle.link == "disconnected"
    stop2.set()
    await asyncio.wait_for(task, 2)


def test_band_control_from_the_hub():
    from pineo_band.hub import HubState
    state = HubState()
    assert state.handle({"type": "band.control", "action": "disconnect"}) == [("band.control", "disconnect")]
    assert state.handle({"type": "band.control", "action": "connect"}) == [("band.control", "connect")]
    assert state.handle({"type": "band.control", "action": "explode"}) == [] and state.handle({"type": "band.control"}) == []
    assert cli.arguments(["--no-autoconnect"]).no_autoconnect is True


def test_gyro_dump_can_be_replayed_and_rotates(tmp_path):
    import math
    from pineo_band.gesture import replay
    lines = []
    link = bandlink.BandLink(17, "ble", 50, GestureDetector(), lambda e: None, None, lambda **f: None, mic=False, log=lines.append)
    link.detector.raw = True
    link.dump = bandlink.GyroDump(tmp_path / "logs")
    one = [900 * math.sin(math.pi * (i + 0.5) / 8) for i in range(8)]
    signal = [0.0] * 20 + one + [-v for v in one] + [0.0] * 15 + one + [-v for v in one] + [0.0] * 60 + [300.0] * 6 + [0.0] * 60
    for n, value in enumerate(signal):
        link._on_motion(SimpleNamespace(t=n * 20.0, n=n, gx=value, gy=1.0, gz=-1.0))
    link._on_motion(SimpleNamespace(t=99999.0, n=None, gx=None, gy=None, gz=None))      # accel-only sample: skipped
    link.dump.close()
    files = sorted((tmp_path / "logs").glob("gyro-*.csv"))
    assert len(files) == 1 and files[0].read_text().splitlines()[0] == "time_s,t_ms,n,gx,gy,gz"
    events, out = replay(files[0], link.detector.p, raw=True)
    assert [e.name for e in events] == ["double"] and sum("REJECTED (refractory" in line for line in out) == 1
    assert sum("GESTURE REJECTED (refractory" in line for line in lines) == 1           # the same near miss, live
    small = bandlink.GyroDump(tmp_path / "rot")
    small.MAX_BYTES = 200
    for n in range(40):
        small.write(n * 20.0, n, 1.0, 2.0, 3.0)
    small.close()
    assert len(list((tmp_path / "rot").glob("gyro-*.csv"))) >= 2
    broken = bandlink.GyroDump("/dev/null/nope")
    broken.write(1.0, 1, 1.0, 2.0, 3.0)                                                # never raises
    assert broken.folder is None
