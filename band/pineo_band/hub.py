"""The hub side: WebSocket /ws?role=band with auto-reconnect, the knowledge base, and the
little state the band process needs (is a bug open, what is on the phone's screen)."""
import asyncio
import json
import time

import aiohttp

RETRY_SECONDS = 1.0
KEEP = ("gesture", "assist", "assist.none", "bug.enrich", "ticket.complete")      # worth sending late; the rest is live-only
KEEP_SECONDS = {"gesture": 3.0, "assist": 8.0, "assist.none": 8.0, "bug.enrich": 600.0, "ticket.complete": 3.0}
MAX_OUTBOX = 20
CATCH_UP_MS = 120000          # a ticket submitted this recently without a write-up is written up when we (re)connect
MAX_QUEUE = 200               # messages waiting for the socket; the oldest is dropped when it is full
SEND_TIMEOUT = 2.0            # a socket that takes longer than this to accept one message is treated as dead


def now_ms() -> int:
    return int(time.time() * 1000)


class HubState:
    """What the hub told us. handle() is pure: message in, list of things that happened out."""

    def __init__(self) -> None:
        self.kb = []
        self.app_context = None
        self.open_bug_id = None
        self.session = None              # {"id", "startedAt"} while a debugging session runs (the hub decides)
        self.closing = False             # a submit was accepted; the hub is waiting for the last words
        self.context_at_open = None

    @property
    def bug_open(self) -> bool:
        """A ticket exists that speech belongs to (also while it is closing)."""
        return self.open_bug_id is not None

    @property
    def gesture_open(self) -> bool:
        """What the gesture detector should believe: a closing ticket no longer takes a submit."""
        return self.open_bug_id is not None and not self.closing

    @property
    def kb_ids(self) -> set:
        return {e["id"] for e in self.kb}

    def set_kb(self, found) -> bool:
        """Keep only well-formed entries; anything else from the wire is dropped."""
        if not isinstance(found, list):
            return False
        clean = []
        for entry in found:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
                clean.append(entry)
        self.kb = clean
        return True

    def handle(self, msg) -> list:
        """-> [(what, payload)]: ("bug.opened", id), ("bug.submitted", {"bugId", "bug"}),
        ("bug.cancelled", id), ("bug.state", bool) whenever open/closed changed."""
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            return []
        kind, was, out = msg["type"], self.gesture_open, []
        if kind == "hello":
            state = msg.get("state") if isinstance(msg.get("state"), dict) else {}
            bug_id = state.get("openBugId")
            self.open_bug_id = bug_id if isinstance(bug_id, str) and bug_id else None
            self.session = state["session"] if isinstance(state.get("session"), dict) else None
            out.append(("session", self.session))
            bugs = state.get("bugs") if isinstance(state.get("bugs"), list) else []
            self.closing = any(isinstance(b, dict) and b.get("id") == self.open_bug_id and b.get("closing") is True for b in bugs) \
                if self.open_bug_id else False
            # Tickets submitted while we were not connected (a restart of this process or of the hub) never
            # reached us as bug.submitted: write them up now if they are recent and still have no title.
            now = msg.get("ts") if isinstance(msg.get("ts"), (int, float)) else now_ms()
            for bug in bugs:
                if (isinstance(bug, dict) and bug.get("status") == "submitted" and isinstance(bug.get("id"), str)
                        and not bug.get("title") and isinstance(bug.get("submittedAt"), (int, float))
                        and 0 <= now - bug["submittedAt"] < CATCH_UP_MS):
                    out.append(("bug.submitted", {"bugId": bug["id"], "bug": bug, "missed": True}))
            if isinstance(state.get("appContext"), dict):
                self.app_context = state["appContext"]
        elif kind == "app.context":
            self.app_context = {k: v for k, v in msg.items() if k not in ("type", "ts")}
        elif kind == "bug.opened":
            bug_id = msg.get("bugId")
            if isinstance(bug_id, str) and bug_id:
                self.open_bug_id = bug_id
                self.closing = False
                self.context_at_open = self.app_context
                out.append(("bug.opened", bug_id))
        elif kind == "band.control":
            if msg.get("action") in ("connect", "disconnect"):
                out.append(("band.control", msg["action"]))
        elif kind == "session.started":
            self.session = {"id": msg.get("sessionId"), "startedAt": msg.get("startedAt")}
            out.append(("session.started", self.session))
        elif kind == "session.ended":
            self.session = None
            out.append(("session.ended", {"id": msg.get("sessionId"), "tickets": msg.get("tickets")}))
        elif kind == "bug.closing":
            bug_id = msg.get("bugId")
            if self.open_bug_id is not None and (bug_id == self.open_bug_id or not isinstance(bug_id, str)):
                self.closing = True
                out.append(("bug.closing", self.open_bug_id))
        elif kind in ("bug.submitted", "bug.cancelled"):
            bug_id = msg.get("bugId")
            if self.open_bug_id is not None and (bug_id == self.open_bug_id or not isinstance(bug_id, str)):
                self.open_bug_id = None
                self.closing = False
            if kind == "bug.submitted" and isinstance(bug_id, str) and bug_id:
                bug = msg.get("bug") if isinstance(msg.get("bug"), dict) else None
                out.append(("bug.submitted", {"bugId": bug_id, "bug": bug}))
            elif kind == "bug.cancelled":
                out.append(("bug.cancelled", bug_id))
        if self.gesture_open != was or kind == "hello":
            out.append(("bug.state", self.gesture_open))
        return out


class HubClient:
    def __init__(self, base_url: str, log=print) -> None:
        self.base_url = base_url.rstrip("/")
        self.state = HubState()
        self.log = log
        self.connected = asyncio.Event()
        self.listeners = []              # fn(what, payload), called in the event loop
        self._ws = None
        self._session = None
        self._outbox = []                # (expires, text)
        self._queue = None               # asyncio.Queue of text for the connected socket (made per connection)
        self.dropped = 0
        self._band_state = None
        self._stop = False
        self._told_down = False
        self.sent = []                   # (type, fields) of everything given to send(): for tests and --say output

    @property
    def ws_url(self) -> str:
        return ("wss://" if self.base_url.startswith("https://") else "ws://") + self.base_url.split("://", 1)[1] + "/ws?role=band"

    # ---- sending (never blocks, never raises)

    def send(self, kind: str, /, **fields) -> bool:
        """True when it went out now. Gestures, answers and write-ups are kept briefly and
        sent after a reconnect; transcripts are live-only."""
        fields = {k: v for k, v in fields.items() if v is not None}
        self.sent.append((kind, fields))
        text = json.dumps(dict(type=kind, ts=now_ms(), **fields), ensure_ascii=False)
        if kind == "band.state":
            self._band_state = fields
        ws, queue = self._ws, self._queue
        if ws is not None and not ws.closed and queue is not None:
            if queue.full():              # never wait, never grow: the oldest goes
                try:
                    queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(text)
            return True
        if kind in KEEP:
            self._outbox.append((time.time() + KEEP_SECONDS[kind], text))
            del self._outbox[:-MAX_OUTBOX]
        return False

    async def _send(self, ws, text) -> bool:
        try:
            await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT)
            return True
        except Exception:
            return False                  # the reader loop notices the dead link and reconnects

    async def _sender(self, ws, queue) -> None:
        """One task per connection drains the queue, so nothing else ever waits on the socket."""
        while True:
            text = await queue.get()
            if not await self._send(ws, text):
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    # ---- HTTP

    async def _get(self, path):
        try:
            async with self._session.get(self.base_url + path, timeout=aiohttp.ClientTimeout(total=4)) as answer:
                if answer.status != 200:
                    return None
                return await answer.json(content_type=None)
        except Exception:
            return None

    async def fetch_kb(self) -> bool:
        found = await self._get("/api/kb")           # async HTTP with a 4 s limit: never blocks the loop
        if isinstance(found, dict):       # tolerate {"elements": [...]}
            found = found.get("elements") or found.get("kb")
        if found is None or not self.state.set_kb(found):
            self.log("hub: no element knowledge base at /api/kb yet (questions cannot be answered until there is one).")
            return False
        self.log("hub: knowledge base loaded, %d elements." % len(self.state.kb))
        return True

    async def fetch_bug(self, bug_id: str):
        found = await self._get("/api/bugs/" + bug_id)
        if isinstance(found, dict) and isinstance(found.get("bug"), dict):
            found = found["bug"]
        return found if isinstance(found, dict) and found.get("id") == bug_id else None

    # ---- the link

    def _emit(self, events) -> None:
        for what, payload in events:
            for listener in self.listeners:
                try:
                    listener(what, payload)
                except Exception as problem:
                    self.log("hub: a listener failed on %s: %s" % (what, problem))

    async def run(self) -> None:
        """Connect, read, reconnect, for ever. Works when the hub starts after us."""
        self._session = aiohttp.ClientSession()
        try:
            while not self._stop:
                sender = None
                try:
                    async with self._session.ws_connect(self.ws_url, heartbeat=15, timeout=aiohttp.ClientWSTimeout(ws_close=3)) as ws:
                        self._told_down = False
                        self.log("hub: connected to %s" % self.base_url)
                        await self.fetch_kb()
                        if self._band_state is not None:      # first, and in order: who we are, then what was kept
                            await self._send(ws, json.dumps(dict(type="band.state", ts=now_ms(), **self._band_state)))
                        now, waiting, self._outbox = time.time(), self._outbox, []
                        for expires, text in waiting:
                            if expires > now:
                                await self._send(ws, text)
                        self._queue = asyncio.Queue(MAX_QUEUE)
                        sender = asyncio.ensure_future(self._sender(ws, self._queue))
                        self._ws = ws                      # from here send() queues for this socket
                        self.connected.set()
                        async for message in ws:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                data = json.loads(message.data)
                            except ValueError:
                                continue
                            self._emit(self.state.handle(data))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                finally:
                    self._ws, self._queue = None, None
                    if sender is not None:
                        sender.cancel()
                    was = self.connected.is_set()
                    self.connected.clear()
                if self._stop:
                    break
                if was or not self._told_down:
                    self._told_down = True
                    self.log("hub: not reachable at %s; trying again every second (start it with hub/run.sh)." % self.base_url)
                await asyncio.sleep(RETRY_SECONDS)
        finally:
            await self._session.close()

    async def close(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None:
            await asyncio.sleep(0.15)     # let the last sends leave
            try:
                await ws.close()
            except Exception:
                pass
