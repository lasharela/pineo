import asyncio
import json

from aiohttp import web

from pineo_band.hub import HubClient, HubState


# ---- the state, pure

def test_hello_sets_everything():
    state = HubState()
    events = state.handle({"type": "hello", "state": {"openBugId": "bug_1", "appContext": {"screen": "cards"}, "bugs": []}})
    assert state.bug_open and state.open_bug_id == "bug_1" and state.app_context == {"screen": "cards"}
    assert events == [("session", None), ("bug.state", True)]
    assert state.handle({"type": "hello", "state": {"openBugId": None}}) == [("session", None), ("bug.state", False)]
    assert not state.bug_open


def test_open_submit_cancel_transitions():
    state = HubState()
    state.handle({"type": "app.context", "ts": 5, "screen": "transfer", "visibleElementIds": ["a"]})
    assert state.app_context == {"screen": "transfer", "visibleElementIds": ["a"]}
    assert state.handle({"type": "bug.opened", "bugId": "b1"}) == [("bug.opened", "b1"), ("bug.state", True)]
    assert state.context_at_open == {"screen": "transfer", "visibleElementIds": ["a"]}
    bug = {"id": "b1", "transcript": []}
    assert state.handle({"type": "bug.submitted", "bugId": "b1", "bug": bug}) == \
        [("bug.submitted", {"bugId": "b1", "bug": bug}), ("bug.state", False)]
    state.handle({"type": "bug.opened", "bugId": "b2"})
    assert state.handle({"type": "bug.cancelled", "bugId": "b2"}) == [("bug.cancelled", "b2"), ("bug.state", False)]
    # a late message about another bug does not close the open one
    state.handle({"type": "bug.opened", "bugId": "b3"})
    events = state.handle({"type": "bug.submitted", "bugId": "b1"})
    assert state.open_bug_id == "b3" and events == [("bug.submitted", {"bugId": "b1", "bug": None})]


def test_junk_from_the_wire_is_ignored():
    state = HubState()
    for junk in (None, [], "x", {}, {"type": 5}, {"type": "bug.opened"}, {"type": "bug.opened", "bugId": 7},
                 {"type": "hello", "state": "no"}, {"type": "something.new", "x": 1}, {"type": "bug.submitted"}):
        state.handle(junk)
    assert not state.bug_open
    assert state.set_kb("nope") is False
    assert state.set_kb([{"id": "a"}, {"id": ""}, {"label": "x"}, 4]) and state.kb_ids == {"a"}


# ---- the client, against a hub on a loopback port

class FakeHub:
    def __init__(self, kb=None):
        self.kb = kb if kb is not None else [{"id": "card-block-toggle", "screen": "cards", "label": "Block card"}]
        self.received, self.sockets, self.hello_state = [], [], {"openBugId": None, "appContext": {"screen": "home"}}
        self.bug = {"id": "b1", "transcript": [{"text": "x"}]}
        self.runner = None

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/api/kb", lambda r: web.json_response(self.kb) if self.kb else web.json_response({"error": "none"}, status=404))
        app.router.add_get("/api/bugs/{id}", lambda r: web.json_response(self.bug) if r.match_info["id"] == "b1" else web.json_response({}, status=404))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return "http://127.0.0.1:%d" % self.port

    async def ws(self, request):
        assert request.query.get("role") == "band"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "hello", "ts": 1, "state": self.hello_state})
        async for message in ws:
            self.received.append(json.loads(message.data))
        return ws

    async def push(self, msg):
        for ws in self.sockets:
            if not ws.closed:
                await ws.send_json(msg)

    async def stop(self):
        for ws in self.sockets:
            await ws.close()
        await self.runner.cleanup()


async def until(check, seconds=3.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_client_connects_fetches_kb_and_follows_state():
    hub = FakeHub()
    url = await hub.start()
    client = HubClient(url, log=lambda text: None)
    seen = []
    client.listeners.append(lambda what, payload: seen.append((what, payload)))
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.wait_for(client.connected.wait(), 3)
        assert client.state.kb_ids == {"card-block-toggle"}
        assert await until(lambda: client.state.app_context == {"screen": "home"})
        assert client.send("band.state", connected=True, listening=True, battery=None, transport="ble") is True
        assert client.send("gesture", name="open", confidence=0.9) is True
        assert client.send("bug.enrich", bugId="b1", kind="bug", title="t") is True      # a field called "kind" is fine
        assert await until(lambda: len(hub.received) == 3)
        assert hub.received[0] == {"type": "band.state", "ts": hub.received[0]["ts"], "connected": True, "listening": True, "transport": "ble"}
        assert hub.received[1]["type"] == "gesture" and hub.received[1]["name"] == "open" and isinstance(hub.received[1]["ts"], int)
        assert hub.received[2]["kind"] == "bug" and hub.received[2]["type"] == "bug.enrich"
        await hub.push({"type": "bug.opened", "bugId": "b1"})
        assert await until(lambda: client.state.bug_open)
        await hub.push({"type": "bug.submitted", "bugId": "b1", "bug": hub.bug})
        assert await until(lambda: not client.state.bug_open)
        assert ("bug.opened", "b1") in seen and ("bug.submitted", {"bugId": "b1", "bug": hub.bug}) in seen
        assert (await client.fetch_bug("b1")) == hub.bug and (await client.fetch_bug("nope")) is None
    finally:
        await client.close()
        task.cancel()
        await hub.stop()


async def test_client_survives_a_hub_that_starts_late_and_restarts(unused_tcp_port):
    url = "http://127.0.0.1:%d" % unused_tcp_port
    lines = []
    client = HubClient(url, log=lines.append)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.3)
        assert not client.connected.is_set()
        assert client.send("transcript", text="lost", final=False) is False       # live-only: dropped
        assert client.send("band.state", connected=True, listening=True, transport="ble") is False
        assert client.send("gesture", name="open") is False                        # kept for a few seconds
        hub = FakeHub()
        hub.hello_state = {"openBugId": "b9", "appContext": None}
        await hub.start(unused_tcp_port)
        await asyncio.wait_for(client.connected.wait(), 4)
        assert await until(lambda: len(hub.received) >= 2)
        assert [m["type"] for m in hub.received] == ["band.state", "gesture"]
        assert await until(lambda: client.state.open_bug_id == "b9")
        await hub.stop()                                                            # the hub goes away...
        assert await until(lambda: not client.connected.is_set())
        hub2 = FakeHub(kb=[])                                                       # ...and comes back, with no kb yet
        await hub2.start(unused_tcp_port)
        await asyncio.wait_for(client.connected.wait(), 4)
        assert await until(lambda: any(m["type"] == "band.state" for m in hub2.received))   # told again who we are
        assert await until(lambda: client.state.open_bug_id is None)
        assert client.state.kb_ids == {"card-block-toggle"}                         # the old kb is kept rather than emptied
        assert any("not reachable" in line for line in lines)
        await hub2.stop()
    finally:
        await client.close()
        task.cancel()


def test_closing_window():
    state = HubState()
    state.handle({"type": "bug.opened", "bugId": "b1"})
    # the submit gesture was accepted: speech still belongs to the ticket, but a second submit must not be sent
    assert state.handle({"type": "bug.closing", "bugId": "b1"}) == [("bug.closing", "b1"), ("bug.state", False)]
    assert state.bug_open and not state.gesture_open
    assert state.handle({"type": "bug.submitted", "bugId": "b1", "bug": {"id": "b1"}}) == [("bug.submitted", {"bugId": "b1", "bug": {"id": "b1"}})]
    assert not state.bug_open and not state.closing
    state.handle({"type": "bug.opened", "bugId": "b2"})
    state.handle({"type": "bug.closing", "bugId": "b2"})
    assert state.handle({"type": "bug.cancelled", "bugId": "b2", "reason": "empty"}) == [("bug.cancelled", "b2")]
    assert not state.bug_open and not state.closing and not state.gesture_open
    # a new ticket opened while the old one was closing
    state.handle({"type": "bug.opened", "bugId": "b3"})
    state.handle({"type": "bug.closing", "bugId": "b3"})
    assert state.handle({"type": "bug.opened", "bugId": "b4"})[-1] == ("bug.state", True)
    assert state.gesture_open and state.open_bug_id == "b4"
    state.handle({"type": "bug.closing", "bugId": "b3"})            # about another ticket: ignored
    assert state.gesture_open
    # hello while a ticket is closing
    fresh = HubState()
    fresh.handle({"type": "hello", "state": {"openBugId": "b9", "bugs": [{"id": "b9", "closing": True}]}})
    assert fresh.bug_open and not fresh.gesture_open


async def test_send_never_blocks_and_drops_the_oldest(monkeypatch):
    from pineo_band import hub as hub_module
    monkeypatch.setattr(hub_module, "MAX_QUEUE", 5)
    client = HubClient("http://127.0.0.1:1", log=lambda text: None)
    client._ws = type("Ws", (), {"closed": False})()               # a socket nobody is draining
    client._queue = asyncio.Queue(5)
    for i in range(50):
        assert client.send("transcript", text=str(i), final=False) is True
    assert client._queue.qsize() == 5 and client.dropped == 45
    assert json.loads(client._queue.get_nowait())["text"] == "45"   # the newest five were kept


def test_sessions_are_tracked():
    state = HubState()
    assert ("session", None) in state.handle({"type": "hello", "state": {"session": None}})
    assert state.handle({"type": "session.started", "sessionId": "s1", "startedAt": 5}) == [("session.started", {"id": "s1", "startedAt": 5})]
    assert state.session == {"id": "s1", "startedAt": 5}
    assert state.handle({"type": "session.ended", "sessionId": "s1", "endedAt": 9, "tickets": 2}) == [("session.ended", {"id": "s1", "tickets": 2})]
    assert state.session is None
    fresh = HubState()
    fresh.handle({"type": "hello", "state": {"session": {"id": "s2", "startedAt": 1}}})
    assert fresh.session["id"] == "s2"


def test_hello_catches_up_on_tickets_submitted_while_away():
    now = 1_000_000
    bugs = [{"id": "old", "status": "submitted", "title": "", "submittedAt": now - 500_000},
            {"id": "done", "status": "submitted", "title": "Has a title", "submittedAt": now - 5_000},
            {"id": "missed", "status": "submitted", "title": "", "submittedAt": now - 5_000, "transcript": [{"text": "x"}]},
            {"id": "open", "status": "open", "title": ""}, "junk"]
    events = HubState().handle({"type": "hello", "ts": now, "state": {"bugs": bugs, "openBugId": "open"}})
    missed = [payload for what, payload in events if what == "bug.submitted"]
    assert [m["bugId"] for m in missed] == ["missed"] and missed[0]["missed"] is True and missed[0]["bug"]["transcript"]
