"""The band's side: the team's band and no other, microphone and gyro at the same time,
auto-reconnect. Uses the kit's async Band, so everything runs in the one event loop."""
import asyncio
import sys
import time

from . import config

RETRY_SECONDS = 2.0
STARTUP_SECONDS = 3.0         # a stream-dropped notice this soon after the streams start is only logged with -v


def load_kit():
    """Make the kit's library importable where it is; nothing is copied from it."""
    folder = config.kit_dir() / "python"
    if not (folder / "vexo_open" / "__init__.py").is_file():
        raise config.ConfigError("The Vexo kit was not found at %s (set PINEO_KIT_DIR to its folder)." % config.kit_dir())
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import vexo_open
    return vexo_open


async def connect(number: int, transport: str):
    """The team's band, and no other. `number` comes from PINEO_BAND_NUMBER."""
    vexo_open = load_kit()
    options = {}
    if transport == "sim":                           # the pretend band speaks the kit's sample recording
        from vexo_open import audio
        sample = config.kit_dir() / "dashboard" / "speech" / "sample-speech.opus"
        found = sorted((config.kit_dir() / "dashboard" / "speech").glob("*.opus"))
        if sample.is_file() or found:
            options["audio_packets"] = audio.ogg_packets(str(sample if sample.is_file() else found[0]))
    band = await vexo_open.Band.connect(number, transport=transport, auto_reconnect=True, **options)
    if band.number != number:                        # the library checks this too; never work on another band
        found = band.number
        await band.disconnect()
        raise config.ConfigError("The band that answered is number %s, not %d: disconnected, nothing changed on it."
                                 % (found, number))
    return band


FATAL_CODES = ("wrong-band", "bad-arg", "no-bleak", "no-pyserial", "bad-transport")   # trying again cannot help


HINT = "Double-tap the band so it blinks blue"


class Released(Exception):
    """Connecting was called off (Disconnect on the dashboard, or the program is stopping)."""


async def connect_patiently(number: int, transport: str, log=print, connect=None, stop=None, sleep=asyncio.sleep, tries=None,
                            on_attempt=None):
    """connect(), tried again for as long as it takes. Over Bluetooth the band is only findable for
    60 seconds after a double-tap, and a weak link can fail in other ways (a timeout while asking the
    band who it is, a dropped connection): none of that may end the program on stage. Always the same
    number. Only what a retry cannot fix ends it: a band with another number, a bad number, no
    Bluetooth library."""
    connect = connect or globals()["connect"]
    attempt = 0
    while True:
        attempt += 1
        if stop is not None and stop.is_set():
            raise Released("not connecting any more")
        try:
            return await connect(number, transport)
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            code = getattr(problem, "code", None)
            if on_attempt:
                on_attempt(code)
            if isinstance(problem, config.ConfigError) or code in FATAL_CODES or (tries and attempt >= tries):
                raise
            if stop is not None and stop.is_set():
                raise Released("not connecting any more") from None
            if code == "not-found-nearby":
                log("%s" % problem)
                log(">>> DOUBLE-TAP YOUR BAND NOW (it blinks blue and can be found for 60 seconds). Close the "
                    "dashboard tab or any other program connected to it. Looking again (attempt %d)... (Ctrl+C to "
                    "give up, --usb to use the cable)" % (attempt + 1))
            else:
                log("band: connecting failed (%s): %s  Trying again in %g s (attempt %d). If it keeps failing, "
                    "double-tap the band." % (code or type(problem).__name__, problem, RETRY_SECONDS, attempt + 1))
            await sleep(RETRY_SECONDS)


class GyroDump:
    """Every gyro sample to band/logs/gyro-<start>.csv, so a missed gesture can be replayed offline:
        python -m pineo_band --replay band/logs/gyro-<start>.csv -v
    Columns time_s,t_ms,n,gx,gy,gz (t_ms is the band's clock; it starts again after a reconnect, which
    --replay handles). A new file is started after MAX_BYTES. Never allowed to break the motion handler."""

    MAX_BYTES = 20 * 1024 * 1024

    def __init__(self, folder) -> None:
        self.folder, self.file, self.path, self.size, self.rows = folder, None, None, 0, 0

    def _open(self) -> None:
        from pathlib import Path
        folder = Path(self.folder)
        folder.mkdir(parents=True, exist_ok=True)
        name = "gyro-%s" % time.strftime("%Y%m%d-%H%M%S")
        self.path = folder / (name + ".csv")
        count = 1
        while self.path.exists():
            count += 1
            self.path = folder / ("%s-%d.csv" % (name, count))
        self.file = open(self.path, "w", buffering=1 << 16)
        header = "time_s,t_ms,n,gx,gy,gz\n"
        self.file.write(header)
        self.size = len(header)

    def write(self, t, n, gx, gy, gz) -> None:
        if self.folder is None or gx is None:
            return
        try:
            if self.file is None or self.size >= self.MAX_BYTES:
                self.close()
                self._open()
            row = "%.3f,%.3f,%s,%s,%s,%s\n" % (time.time(), t, "" if n is None else n, gx, gy, gz)
            self.file.write(row)
            self.size += len(row)
            self.rows += 1
            if self.rows % 100 == 0:                      # every couple of seconds: on disk even if the program is killed
                self.file.flush()
        except Exception:
            self.folder = None                            # a full disk or a bad folder: stop dumping, keep detecting

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None


class BandLink:
    def __init__(self, number: int, transport: str, rate: int, detector, on_gesture, on_pcm, on_state,
                 mic: bool = True, log=print, verbose: bool = False) -> None:
        self.number, self.transport, self.rate, self.mic = number, transport, rate, mic
        self.detector, self.on_gesture, self.on_pcm, self.on_state = detector, on_gesture, on_pcm, on_state
        self.log, self.verbose = log, verbose
        self.band = None
        self.decoder = None
        self.battery = None
        self.listening = False
        self.samples = self.packets = 0
        self._stats_at = time.monotonic()
        self._last_n = None
        self.lost_samples = 0
        self._streams_at = time.monotonic()
        self.dropped_notices = 0
        self.wanted = asyncio.Event()    # set: be connected. Clear: released by hand (dashboard), stay idle, do not retry
        self.wanted.set()
        self.link, self.hint = "disconnected", None      # connected | connecting | searching | disconnected
        self._rejected = []
        self.dump = GyroDump(None)       # replaced by cli when a log folder is given
        self.want_mic = False            # the microphone follows the session: on only while one is active
        self._mic_asked_at = None
        self._mic_lock = asyncio.Lock()
        self.mic_on_ms = []              # measured: "microphone on" asked -> first sound packet

    # ---- handlers: short, they only pass things on

    def _on_motion(self, m) -> None:
        self.samples += 1
        n = getattr(m, "n", None)
        if n is not None and self._last_n is not None and n > self._last_n + 1:
            self.lost_samples += n - self._last_n - 1
        self._last_n = n
        self.dump.write(m.t, n, m.gx, m.gy, m.gz)
        for event in self.detector.feed(m.t, m.gx, m.gy, m.gz):
            self.on_gesture(event)
        for _, reason, detail in self.detector.take_rejections():
            now = time.monotonic()
            self._rejected = [at for at in self._rejected if now - at < 1.0]
            if len(self._rejected) < 3:                   # a few lines a second at most
                self._rejected.append(now)
                self.log("GESTURE REJECTED (%s) [%s]" % (reason, detail))

    def _on_audio(self, packet) -> None:
        self.packets += 1
        if self._mic_asked_at is not None:
            took = round((time.monotonic() - self._mic_asked_at) * 1000)
            self._mic_asked_at = None
            self.mic_on_ms.append(took)
            self.log("band: sound is flowing %d ms after the microphone was asked for." % took)
        decoder = self.decoder
        if decoder is None:
            return
        try:
            pcm = decoder.feed(packet)
        except Exception:
            return
        if pcm:
            self.on_pcm(pcm)

    def _on_notice(self, e) -> None:
        kind = getattr(e, "kind", "?")
        if kind != "stream-dropped":
            self.log("band: notice %s" % kind)
            return
        # The band says frames of one stream were lost because the link was too slow (contract: Notice 3,
        # arg = stream bit: 3 is sound, 8 is fast motion). In the first seconds after the streams start the
        # Bluetooth link is still being sped up, so one such notice then is expected and harmless.
        arg = getattr(e, "arg", None)
        stream = {3: "sound", 8: "motion"}.get(arg, "stream %s" % arg)
        early = time.monotonic() - self._streams_at < STARTUP_SECONDS
        self.dropped_notices += 0 if early else 1
        if early:
            if self.verbose:
                self.log("band: (start-up) a few %s frames were lost while the link was settling." % stream)
        else:
            self.log("band: WARNING stream-dropped (%s): the Bluetooth link could not keep up. If it keeps happening: "
                     "move the laptop closer, quiet Wi-Fi traffic, or lower --motion-rate." % stream)

    def _on_battery(self, b) -> None:
        percent = getattr(b, "percent", None)
        if isinstance(percent, (int, float)) and percent != self.battery:
            self.battery = percent
            self._state()

    def _state(self, connected=None) -> None:
        band = self.band
        up = bool(band is not None and band.connected) if connected is None else connected
        if up:
            self.link, self.hint = "connected", None
        elif self.link == "connected":                    # the link went away under us: the kit is getting it back
            self.link = "connecting" if self.wanted.is_set() else "disconnected"
        self.on_state(connected=up, listening=bool(up and self.listening), battery=self.battery, link=self.link, hint=self.hint,
                      transport=getattr(band, "transport_kind", None) or {"serial": "serial", "sim": "sim"}.get(self.transport, "ble"))

    def _new_decoder(self) -> None:
        if not self.mic:
            return
        from vexo_open.audio import PcmDecoder
        self.decoder = PcmDecoder()

    async def _configure(self, band) -> None:
        try:
            await band.configure(motion=self.rate, sensors="gyro", battery=60)
            self.log("band: gyro at %d samples a second (sensors: gyro only)." % self.rate)
        except Exception as problem:
            if getattr(problem, "code", None) not in ("not-available", "bad-arg"):
                raise
            rate = min(self.rate, 100)                    # firmware 1.0: both sensors, 100 at most, no intervals
            await band.configure(motion=rate, battery=True)
            self.log("band: gyro and accelerometer at %d samples a second (this band cannot send the gyro alone)." % rate)
        self.listening = False                            # a fresh link has every stream off
        await self._apply_mic()

    async def set_mic(self, on: bool) -> None:
        """The session started (True) or ended (False). The gyro keeps streaming either way."""
        self.want_mic = bool(on)
        if on and self._mic_asked_at is None and not self.listening:
            self._mic_asked_at = time.monotonic()
        await self._apply_mic()

    async def _apply_mic(self) -> None:
        async with self._mic_lock:
            band = self.band
            if not self.mic or band is None or not band.connected:
                return                                    # applied again after the next (re)connect
            try:
                if self.want_mic and not self.listening:
                    self._new_decoder()                   # the kit's rule: a new decoder for a stream that starts again
                    self._streams_at = time.monotonic()
                    await band.configure(audio=True)      # motion is not mentioned, so it keeps its setting; light: solid blue
                    self.listening = True
                    self.log("MICROPHONE ON (session active). What the band hears is sent over the internet to Deepgram "
                             "(deepgram.com) and, as text, to MiniMax. Tell the people near the band.")
                    self._state()
                elif not self.want_mic and self.listening:
                    await band.configure(audio=False)
                    self.listening = False
                    self._mic_asked_at = None
                    self.log("microphone off (no session). The gyro keeps running: rotate twice to start a session.")
                    self._state()
                    await self._blink(band)
            except Exception as problem:
                self.log("band: could not switch the microphone %s: %s" % ("on" if self.want_mic else "off", problem))

    async def _blink(self, band) -> None:
        """With the microphone off the LED is free: one short green blink says "session over, idle", then dark.
        (At session start the band shows its own solid blue microphone light.) Never allowed to break anything."""
        try:
            await band.set_led("#00ff00", brightness=0.4, pattern="blink", period=300)
            await asyncio.sleep(0.9)
            if not self.listening and not self.want_mic:
                await band.led_off()
        except Exception:
            pass

    # ---- connect / disconnect by hand (the dashboard's buttons)

    def connect_now(self) -> None:
        if not self.wanted.is_set():
            self.log("band: CONNECT asked for: looking for the band again.")
        self.wanted.set()

    def release(self) -> None:
        """Let the band go and stay idle: no retries until connect_now(). Safe at any moment."""
        if self.wanted.is_set():
            self.log("band: DISCONNECT asked for: releasing the band. The program keeps running, idle.")
        self.wanted.clear()

    def _searching(self, code) -> None:
        self.link = "searching" if code == "not-found-nearby" else "connecting"
        self.hint = HINT if code == "not-found-nearby" else None
        self._state(False)

    # ---- the loop

    async def run(self, stop: asyncio.Event) -> None:
        if self.mic:
            self.log("Loading the sound decoder...")
            load_kit()
            self._new_decoder()                           # a few seconds, once, before the microphone goes on
        class Either:                                     # connecting is called off by Ctrl+C or by a manual release
            is_set = staticmethod(lambda: stop.is_set() or not self.wanted.is_set())

        while not stop.is_set():
            if not self.wanted.is_set():                      # released by hand: idle, no retries, until asked again
                self.link, self.hint = "disconnected", None
                self._state(False)
                waiting = [asyncio.ensure_future(self.wanted.wait()), asyncio.ensure_future(stop.wait())]
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for task in waiting:
                    task.cancel()
                continue
            self.log("band: looking for band %d over %s..." % (self.number, {"ble": "Bluetooth", "serial": "the USB cable", "sim": "the pretend band"}[self.transport]))
            self.link, self.hint = "connecting", None
            self._state(False)
            try:
                band = self.band = await connect_patiently(self.number, self.transport, self.log, stop=Either, on_attempt=self._searching)
            except Released:
                continue
            except Exception as problem:
                if stop.is_set():
                    return
                raise                                     # only what a retry cannot fix (see FATAL_CODES)
            if not self.wanted.is_set():                  # released while the connection was being made
                try:
                    await band.disconnect()
                except Exception:
                    pass
                self.band = None
                continue
            self._last_n = None
            band.on("motion", self._on_motion)
            band.on("audio", self._on_audio)
            band.on("battery", self._on_battery)
            band.on("reconnecting", lambda e: (self.log("band: link lost, reconnecting for up to %s s..." % getattr(e, "seconds", "?")),
                                               self._state(False)))

            def back(e):
                self._last_n = None
                self._streams_at = time.monotonic()
                self._new_decoder()                       # the kit's rule: a new decoder after the stream started again
                self.listening = bool(getattr(band, "streams", {}).get("audio")) if isinstance(getattr(band, "streams", None), dict) else self.listening
                asyncio.ensure_future(self._apply_mic())  # the session may have started or ended meanwhile
                failed = getattr(e, "failed", None)
                self.log("band: reconnected after %.1f s%s." % (getattr(e, "seconds", 0) or 0, (" (refused: %s)" % failed) if failed else ""))
                self._state(True)
            band.on("reconnected", back)
            band.on("disconnect", lambda e: self.log("band: disconnected (%s)." % getattr(e, "reason", "?")))
            band.on("notice", self._on_notice)
            band.on("stream-taken", lambda e: self.log("band: %s" % getattr(e, "message", "another program took a stream")))
            self._streams_at = time.monotonic()
            try:
                await self._configure(band)
            except Exception as problem:
                self.log("band: could not switch the streams on: %s" % problem)
            self.log("Connected to %s. Rotate twice to start or end a session (the microphone is on only during one); "
                     "once to open a ticket." % band.name)
            self._state()
            try:
                while not stop.is_set() and self.wanted.is_set() and (band.connected or getattr(band, "reconnecting", False)):
                    await asyncio.sleep(0.25)
                    self._stats()
            except asyncio.CancelledError:
                raise
            except Exception as problem:                  # nothing in the link may take the program down
                self.log("band: link trouble (%s): starting over." % problem)
            finally:
                released = not self.wanted.is_set()
                if released or stop.is_set():             # leave the band dark and quiet before letting it go
                    try:
                        if self.listening:
                            await band.configure(audio=False)
                        await band.led_off()
                    except Exception:
                        pass
                self.listening = False
                self._mic_asked_at = None
                self.link, self.hint = ("disconnected" if released or stop.is_set() else "connecting"), None
                self._state(False)
                try:
                    await band.disconnect()               # the microphone and the streams go off with the link
                except Exception:
                    pass
                self.band = None
            if released:
                self.log("band: released. Press Connect on the dashboard to use it again.")
            elif not stop.is_set():
                self.log("band: the link is gone for good; starting over.")

    def _stats(self) -> None:
        now = time.monotonic()
        if not self.verbose or now - self._stats_at < 10:
            return
        seconds, self._stats_at = now - self._stats_at, now
        lost = getattr(self.decoder, "lost", 0) if self.decoder else 0
        self.log("band: %.0f gyro samples/s (%d lost), %.0f sound packets/s (%d lost so far)"
                 % (self.samples / seconds, self.lost_samples, self.packets / seconds, lost))
        self.samples = self.packets = 0
