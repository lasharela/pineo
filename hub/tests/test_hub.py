"""Hub tests. Run from pineo/: .venv/bin/python -m pytest hub/tests -q"""
from __future__ import annotations

import asyncio
import json

import pytest

from hub.server import create_app

@pytest.fixture
def paths(tmp_path):
    dash = tmp_path / "dashboard"
    dash.mkdir()
    (dash / "index.html").write_text("<h1>dash</h1>")
    (dash / "app.js").write_text("console.log(1)")
    return {
        "data_dir": tmp_path / "data",
        "app_dist": tmp_path / "dist",
        "dashboard_dir": dash,
        "kb_path": tmp_path / "elements.json",
        "submit_grace": 0.05,  # the real default is 1.2 s
        "ticket_idle": 60, "ticket_empty_idle": 60,  # idle auto-close has its own tests
        # closing-window timings scaled down; late finals (amendments) are off except in their own tests
        "timing": {"speech_settle": 0.05, "quiet_window": 0.05, "close_cap": 3.0, "late_final": 0,
                   "next_grace": 0.1, "next_cap": 1.0},
    }


@pytest.fixture
async def client(aiohttp_client, paths):
    return await aiohttp_client(create_app(**paths))


async def connect(client, role):
    """Open a socket and consume its hello."""
    ws = await client.ws_connect(f"/ws?role={role}")
    hello = await ws.receive_json(timeout=2)
    assert hello["type"] == "hello" and hello["role"] == role
    ws.hello = hello
    return ws


async def drain(ws, timeout=0.15, sessions=False):
    """Everything the socket has received, until it goes quiet (session.* only on request)."""
    out = []
    while True:
        try:
            out.append(await ws.receive_json(timeout=timeout))
        except asyncio.TimeoutError:
            return out if sessions else [m for m in out if not m["type"].startswith("session.")]


def types(msgs):
    return [m["type"] for m in msgs if m["type"] != "clients" and not m["type"].startswith("session.")]


async def post(client, path, body):
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


# ----------------------------------------------------------------- connect / hello


async def test_hello_carries_full_state(client):
    ws = await connect(client, "dashboard")
    state = ws.hello["state"]
    assert state["bugs"] == [] and state["openBugId"] is None
    assert state["band"] == {"connected": False, "listening": False, "battery": None, "transport": None}
    assert state["appContext"] is None and state["assists"] == []
    assert state["clients"] == {"app": 0, "dashboard": 1, "band": 0, "agent": 0}
    assert isinstance(ws.hello["ts"], int)
    assert await (await client.get("/api/state")).json() == state


async def test_bad_role_is_rejected(client):
    assert (await client.get("/ws?role=admin")).status == 400
    assert (await client.get("/ws")).status == 400


# ----------------------------------------------------------------- state machine


async def test_full_bug_lifecycle_and_routing_per_role(client):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await app.send_json({"type": "app.context", "screen": "cards", "visibleElementIds": ["card-block-toggle"],
                         "lastTappedElementId": "card-block-toggle", "viewport": {"w": 390, "h": 844}})
    for ws in (dash, band, app):
        await drain(ws)

    # open
    await band.send_json({"type": "gesture", "name": "open", "confidence": 0.9})
    d = await drain(dash)
    assert types(d) == ["gesture", "bug.upsert"]
    assert d[0]["accepted"] is True and d[0]["confidence"] == 0.9
    bug = d[1]["bug"]
    assert bug["status"] == "open" and bug["screen"] == "cards"
    assert bug["elementIds"] == [] and bug["contextAtOpen"]["screen"] == "cards"
    assert bug["transcript"] == [] and bug["interactions"] == [] and bug["id"].startswith("bug_")
    assert bug["kind"] == "" and bug["steps"] == [] and bug["fix"] == {"state": "", "summary": "", "files": [], "ms": 0}
    a = await drain(app)
    assert types(a) == ["bug.opened"] and a[0]["bugId"] == bug["id"]
    b = await drain(band)
    assert types(b) == ["bug.opened"] and b[0]["bugId"] == bug["id"]

    # interim is forwarded but not stored; final is appended
    await band.send_json({"type": "transcript", "text": "the block", "final": False})
    await band.send_json({"type": "transcript", "text": "The block card switch does nothing", "final": True})
    d = await drain(dash)
    assert types(d) == ["transcript", "transcript", "bug.upsert"]
    assert d[0]["final"] is False and d[1]["final"] is True and d[1]["bugId"] == bug["id"]
    assert [t["text"] for t in d[2]["bug"]["transcript"]] == ["The block card switch does nothing"]
    assert types(await drain(app)) == ["transcript", "transcript"]
    assert await drain(band) == []  # the band never gets its own transcript back

    # submit
    await band.send_json({"type": "gesture", "name": "submit"})
    d = await drain(dash)
    assert types(d) == ["gesture", "bug.upsert", "bug.upsert"]
    assert d[0]["closing"] is True and d[1]["bug"]["status"] == "open" and d[1]["bug"]["closing"] is True
    assert d[2]["bug"]["status"] == "submitted" and d[2]["bug"]["submittedAt"] >= bug["openedAt"]
    assert "closing" not in d[2]["bug"]
    a = await drain(app)
    assert types(a) == ["bug.closing", "bug.submitted"] and "bug" not in a[1]
    b = await drain(band)
    assert types(b) == ["bug.closing", "bug.submitted"]
    assert b[1]["bug"]["transcript"][0]["text"] == "The block card switch does nothing"

    # enrich
    await band.send_json({"type": "bug.enrich", "bugId": bug["id"], "title": "Block card toggle is dead",
                          "summary": "Toggling does nothing.", "severity": "high", "kind": "bug",
                          "elementIds": ["card-block-toggle", "card-list"], "steps": ["Open Cards", "Tap Block card", 3]})
    d = await drain(dash)
    assert types(d) == ["bug.upsert"]
    got = d[0]["bug"]
    assert (got["title"], got["severity"]) == ("Block card toggle is dead", "high")
    assert got["elementIds"] == ["card-block-toggle", "card-list"]
    assert got["kind"] == "bug" and got["steps"] == ["Open Cards", "Tap Block card"]
    assert await drain(app) == []

    state = await (await client.get("/api/state")).json()
    assert state["openBugId"] is None and state["bugs"][0]["id"] == bug["id"]


async def test_second_open_and_stray_submit_are_ignored(client):
    dash, band, app = [await connect(client, r) for r in ("dashboard", "band", "app")]
    await drain(dash)

    await band.send_json({"type": "gesture", "name": "submit"})  # nothing open
    await band.send_json({"type": "gesture", "name": "cancel"})
    d = await drain(dash)
    assert types(d) == ["gesture", "gesture"] and not d[0]["accepted"] and not d[1]["accepted"]
    assert await drain(app) == []

    await band.send_json({"type": "gesture", "name": "open"})
    first = [m for m in await drain(dash) if m["type"] == "bug.upsert"][0]["bug"]
    await band.send_json({"type": "gesture", "name": "open"})  # second open
    d = await drain(dash)
    assert types(d) == ["gesture"] and d[0]["accepted"] is False and d[0]["bugId"] == first["id"]
    assert types(await drain(app)) == ["bug.opened"]  # only one
    bugs = await (await client.get("/api/bugs")).json()
    assert len(bugs) == 1 and bugs[0]["status"] == "open"


async def test_cancel_and_transcript_only_while_open(client):
    dash, band, app = [await connect(client, r) for r in ("dashboard", "band", "app")]
    await band.send_json({"type": "transcript", "text": "where is the block switch", "final": True})
    d = [m for m in await drain(dash) if m["type"] != "clients"]
    assert types(d) == ["transcript"] and d[0]["bugId"] is None  # forwarded, stored nowhere

    await band.send_json({"type": "gesture", "name": "open"})
    await band.send_json({"type": "transcript", "text": "one", "final": True})
    await band.send_json({"type": "gesture", "name": "cancel"})
    await band.send_json({"type": "transcript", "text": "after cancel", "final": True})
    d = await drain(dash)
    last = [m for m in d if m["type"] == "bug.upsert"][-1]["bug"]
    assert last["status"] == "cancelled" and [t["text"] for t in last["transcript"]] == ["one"]
    assert types(await drain(app)) == ["transcript", "bug.opened", "transcript", "bug.cancelled", "transcript"]

    await band.send_json({"type": "gesture", "name": "open"})  # a new one can open after a cancel
    bugs = [m for m in await drain(dash) if m["type"] == "bug.upsert"]
    assert bugs[0]["bug"]["id"] != last["id"] and bugs[0]["bug"]["transcript"] == []


async def test_only_the_band_role_can_drive_gestures(client):
    app, dash = await connect(client, "app"), await connect(client, "dashboard")
    await drain(dash)
    await app.send_json({"type": "gesture", "name": "open"})
    await dash.send_json({"type": "gesture", "name": "open"})
    await dash.send_json({"type": "transcript", "text": "hi", "final": True})
    assert await drain(dash) == [] and await drain(app) == []
    assert await (await client.get("/api/bugs")).json() == []


# ----------------------------------------------------------------- submit: empty tickets and late words


async def test_submit_with_nothing_captured_cancels_instead(client):
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]
    await band.send_json({"type": "gesture", "name": "open"})
    bug_id = (await drain(app))[0]["bugId"]
    await band.send_json({"type": "transcript", "text": "not final", "final": False})  # interim does not count
    await app.send_json({"type": "app.interaction", "bugId": bug_id, "items": [  # neither do these
        {"kind": "screen", "screen": "cards"}, {"kind": "scroll", "screen": "cards"}]})
    for ws in (app, dash, band, agent):
        await drain(ws)

    await band.send_json({"type": "gesture", "name": "submit"})
    d = await drain(dash)
    assert types(d) == ["gesture", "bug.upsert", "bug.upsert"] and d[0]["accepted"] is True
    assert d[2]["bug"]["status"] == "cancelled" and d[2]["bug"]["cancelReason"] == "empty"
    assert d[2]["bug"]["submittedAt"] == 0
    a = await drain(app)
    assert types(a) == ["bug.closing", "bug.cancelled"] and a[1]["reason"] == "empty"
    assert types(await drain(band)) == ["bug.closing", "bug.cancelled"]
    assert types(await drain(agent)) == ["bug.cancelled"]  # never bug.submitted
    assert (await (await client.get("/api/state")).json())["openBugId"] is None

    status, body = await post(client, "/api/debug/gesture", {"name": "open"})  # the next one opens normally
    assert body["accepted"] is True
    status, body = await post(client, "/api/debug/gesture", {"name": "submit"})  # the debug route reports the outcome
    assert body["accepted"] is True and body["bug"]["status"] == "cancelled" and body["openBugId"] is None


async def test_a_tap_a_circle_or_typing_is_enough_to_submit(client):
    app, band = await connect(client, "app"), await connect(client, "band")
    for item in ({"kind": "tap", "elementId": "pay-button"}, {"kind": "circle", "elementIds": ["a"], "path": []},
                 {"kind": "input", "elementId": "amount"}):
        await band.send_json({"type": "gesture", "name": "open"})
        bug_id = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
        await app.send_json({"type": "app.interaction", "bugId": bug_id, "items": [item]})
        await band.send_json({"type": "gesture", "name": "submit"})
        assert types(await drain(app))[-1] == "bug.submitted", item
        assert (await (await client.get(f"/api/bugs/{bug_id}")).json())["status"] == "submitted"


async def test_last_words_arriving_after_the_submit_gesture_are_kept(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "submit_grace": 1.0}))
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]
    await band.send_json({"type": "gesture", "name": "open"})
    bug_id = (await drain(app))[0]["bugId"]
    await drain(dash)

    await band.send_json({"type": "gesture", "name": "submit"})
    a = await drain(app)
    assert types(a) == ["bug.closing"] and a[0]["bugId"] == bug_id  # the phone hears at once, no verdict yet
    assert await drain(agent) == []
    state = await (await client.get("/api/state")).json()
    assert state["openBugId"] == bug_id and state["bugs"][0]["closing"] is True

    await band.send_json({"type": "gesture", "name": "submit"})  # a second submit while closing is ignored
    assert [m for m in await drain(dash) if m["type"] == "gesture"][-1]["accepted"] is False

    await band.send_json({"type": "transcript", "text": "the pay button is cut off", "final": True})
    a = await drain(app)  # well inside the 1 s window: the final ends the wait early
    assert types(a) == ["transcript", "bug.submitted"]
    handed = await drain(agent)
    assert types(handed) == ["bug.submitted"]
    assert [t["text"] for t in handed[0]["bug"]["transcript"]] == ["the pay button is cut off"]

    await band.send_json({"type": "transcript", "text": "too late", "final": True})  # after the verdict
    await drain(app)
    assert len((await (await client.get(f"/api/bugs/{bug_id}")).json())["transcript"]) == 1


async def test_open_or_cancel_during_the_submit_window(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "submit_grace": 1.0}))
    app, band = await connect(client, "app"), await connect(client, "band")
    await band.send_json({"type": "gesture", "name": "open"})
    await band.send_json({"type": "transcript", "text": "first report", "final": True})
    await band.send_json({"type": "gesture", "name": "submit"})
    await band.send_json({"type": "gesture", "name": "open"})  # no waiting: the first is settled, the next opens
    a = [m for m in await drain(app) if m["type"].startswith("bug.")]
    assert [m["type"] for m in a] == ["bug.opened", "bug.closing", "bug.submitted", "bug.opened"]
    assert a[2]["bugId"] == a[0]["bugId"] and a[3]["bugId"] != a[0]["bugId"]

    await band.send_json({"type": "transcript", "text": "second report", "final": True})
    await band.send_json({"type": "gesture", "name": "submit"})
    await band.send_json({"type": "gesture", "name": "cancel"})  # changed their mind inside the window
    a = [m for m in await drain(app) if m["type"].startswith("bug.")]
    assert [m["type"] for m in a] == ["bug.closing", "bug.cancelled"] and a[1]["reason"] == "gesture"
    await drain(app, 1.2)  # the abandoned timer fires and must do nothing
    bugs = await (await client.get("/api/bugs")).json()
    assert [b["status"] for b in bugs] == ["cancelled", "submitted"]


async def test_open_is_announced_before_anything_else(client):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await drain(dash)
    await band.send_json({"type": "gesture", "name": "double"})
    await drain(app)
    await band.send_json({"type": "gesture", "name": "single"})  # no context, no transcript, nothing else needed
    first = await app.receive_json(timeout=0.5)
    assert first["type"] == "bug.opened"
    assert types(await drain(band)) == ["bug.opened"]


async def test_deselected_taps_do_not_count(client):
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]

    async def report(items):
        await band.send_json({"type": "gesture", "name": "open"})
        bug_id = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
        await app.send_json({"type": "app.interaction", "bugId": bug_id, "items": items})
        await band.send_json({"type": "gesture", "name": "submit"})
        await drain(app)
        return await (await client.get(f"/api/bugs/{bug_id}")).json()

    def tap(element_id, selected):
        return {"kind": "tap", "screen": "pay", "elementId": element_id, "selected": selected}

    # selected then deselected, with no speech: nothing is meant, so the report is empty
    bug = await report([{"kind": "screen", "screen": "pay"}, tap("amount", True), tap("amount", False)])
    assert bug["status"] == "cancelled" and bug["cancelReason"] == "empty" and bug["elementIds"] == []
    assert len(bug["interactions"]) == 3  # the history is still kept
    assert "bug.submitted" not in types(await drain(agent))

    # deselected, then chosen again later (by a tap or inside a circle): it counts
    bug = await report([tap("amount", True), tap("send", True), tap("amount", False), tap("amount", True)])
    assert bug["status"] == "submitted" and bug["elementIds"] == ["send", "amount"]
    bug = await report([tap("amount", True), tap("amount", False),
                        {"kind": "circle", "screen": "pay", "elementIds": ["amount", "note"], "path": []}])
    assert bug["status"] == "submitted" and bug["elementIds"] == ["amount", "note"]

    # one of two deselected: only the other stays involved, and the dashboard saw it change live
    await drain(dash)
    bug = await report([tap("amount", True), tap("send", True), tap("send", False)])
    assert bug["status"] == "submitted" and bug["elementIds"] == ["amount"]
    assert [m["bug"]["elementIds"] for m in await drain(dash) if m["type"] == "bug.upsert"][1] == ["amount"]

    # the write-up's element list wins and a late flush does not undo it
    await band.send_json({"type": "bug.enrich", "bugId": bug["id"], "elementIds": ["send", "fee"]})
    await app.send_json({"type": "app.interaction", "bugId": bug["id"], "items": [tap("note", True)]})
    await drain(app)
    assert (await (await client.get(f"/api/bugs/{bug['id']}")).json())["elementIds"] == ["send", "fee"]


# ----------------------------------------------------------------- v3: sessions, ticket.complete, idle timers


async def test_gesture_table_double_and_single(client):
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]
    assert dash.hello["state"]["session"] is None
    await drain(dash)

    await band.send_json({"type": "gesture", "name": "single"})  # no session: ignored
    d = await drain(dash, sessions=True)
    assert types(d) == ["gesture"] and d[0]["accepted"] is False and d[0]["action"] is None
    assert await drain(app, sessions=True) == []

    await band.send_json({"type": "gesture", "name": "double"})  # session starts
    started = [await drain(ws, sessions=True) for ws in (app, dash, band, agent)]
    for msgs in started:
        assert [m["type"] for m in msgs if m["type"] == "session.started"] == ["session.started"]
    session_id = started[0][0]["sessionId"]
    assert isinstance(started[0][0]["startedAt"], int)
    state = await (await client.get("/api/state")).json()
    assert state["session"] == {"id": session_id, "startedAt": started[0][0]["startedAt"]} and state["bugs"] == []

    await band.send_json({"type": "gesture", "name": "single"})  # opens a ticket
    a = await drain(app)
    assert types(a) == ["bug.opened"]
    bug_id = a[0]["bugId"]
    d = await drain(dash)
    assert d[0]["action"] == "ticket.open" and d[1]["bug"]["sessionId"] == session_id

    await band.send_json({"type": "transcript", "text": "the total is wrong", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})  # ticket open: manual submit
    assert types(await drain(app)) == ["transcript", "bug.closing", "bug.submitted"]
    assert types(await drain(agent)) == ["bug.submitted"]
    bug = await (await client.get(f"/api/bugs/{bug_id}")).json()
    assert bug["status"] == "submitted" and bug["closedBy"] == "gesture"

    await band.send_json({"type": "gesture", "name": "single"})  # another ticket in the same session
    second = (await drain(app))[0]["bugId"]
    assert second != bug_id
    await band.send_json({"type": "gesture", "name": "cancel"})
    await drain(app)

    await band.send_json({"type": "gesture", "name": "double"})  # session ends
    for ws in (app, dash, band, agent):
        ended = [m for m in await drain(ws, sessions=True) if m["type"] == "session.ended"]
        assert len(ended) == 1 and ended[0]["sessionId"] == session_id and ended[0]["tickets"] == 1
        assert ended[0]["endedAt"] >= ended[0]["startedAt"]
    state = await (await client.get("/api/state")).json()
    assert state["session"] is None and state["sessions"][0]["id"] == session_id and state["sessions"][0]["tickets"] == 1

    await band.send_json({"type": "gesture", "name": "double"})  # and a new one can start
    again = [m for m in await drain(app, sessions=True) if m["type"] == "session.started"]
    assert again and again[0]["sessionId"] != session_id


async def test_ending_a_session_settles_the_open_ticket_first(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "submit_grace": 0.4}))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "transcript", "text": "the header overlaps", "final": True})
    await drain(app, sessions=True)
    await band.send_json({"type": "gesture", "name": "double"})
    a = await drain(app, 0.15, sessions=True)
    assert [m["type"] for m in a] == ["bug.closing"]  # the session is still live while the ticket settles
    assert (await (await client.get("/api/state")).json())["session"] is not None
    await band.send_json({"type": "transcript", "text": "on small screens", "final": True})  # last words still count
    a = await drain(app, sessions=True)
    assert [m["type"] for m in a] == ["transcript", "bug.submitted", "session.ended"] and a[2]["tickets"] == 1
    handed = [m for m in await drain(agent, sessions=True) if m["type"] == "bug.submitted"][0]["bug"]
    assert [t["text"] for t in handed["transcript"]] == ["the header overlaps", "on small screens"]
    assert handed["closedBy"] == "session-end"

    # an empty open ticket is cancelled, and the session still ends, with no tickets
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "gesture", "name": "double"})
    a = await drain(app, 0.7, sessions=True)
    assert [m["type"] for m in a] == ["session.started", "bug.opened", "bug.closing", "bug.cancelled", "session.ended"]
    assert a[3]["reason"] == "empty" and a[4]["tickets"] == 0
    state = await (await client.get("/api/state")).json()
    assert state["session"] is None and state["openBugId"] is None


async def test_old_gesture_names_act_on_the_ticket_and_start_a_session(client):
    app = await connect(client, "app")
    status, body = await post(client, "/api/debug/gesture", {"name": "open"})  # no session yet
    assert body["accepted"] is True and body["action"] == "ticket.open"
    assert body["session"]["id"] == body["bug"]["sessionId"]
    a = await drain(app, sessions=True)
    assert [m["type"] for m in a] == ["session.started", "bug.opened"]
    status, body = await post(client, "/api/debug/gesture", {"name": "single"})  # debug route takes raw names too
    assert body["action"] == "ticket.submit" and body["bug"]["status"] == "cancelled"  # nothing was captured
    status, body = await post(client, "/api/debug/gesture", {"name": "double"})
    assert body["action"] == "session.end" and body["session"] is None


async def test_ticket_complete_submits_through_the_closing_window(client):
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    bug_id = (await drain(app))[0]["bugId"]
    await band.send_json({"type": "ticket.complete", "bugId": "bug_other", "reason": "x"})  # not the open ticket
    await band.send_json({"type": "transcript", "text": "make the send button green", "final": True})
    await drain(app)
    await band.send_json({"type": "ticket.complete", "bugId": bug_id, "reason": "clear change request"})
    assert types(await drain(app)) == ["bug.closing", "bug.submitted"]
    bug = [m for m in await drain(agent) if m["type"] == "bug.submitted"][0]["bug"]
    assert bug["closedBy"] == "complete" and bug["completeReason"] == "clear change request"
    assert (await (await client.get("/api/state")).json())["session"] is not None  # the session goes on

    await band.send_json({"type": "gesture", "name": "single"})
    await drain(app)
    status, body = await post(client, "/api/debug/complete", {})  # empty ticket: the empty rule still applies
    assert status == 200 and body["bug"]["status"] == "cancelled" and body["bug"]["cancelReason"] == "empty"
    status, body = await post(client, "/api/debug/complete", {})
    assert status == 400  # nothing open


async def test_idle_ticket_with_content_is_submitted(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "ticket_idle": 0.5, "ticket_empty_idle": 30}))
    app, band = await connect(client, "app"), await connect(client, "band")
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    bug_id = (await drain(app))[0]["bugId"]
    await band.send_json({"type": "transcript", "text": "the list flickers", "final": True})
    for _ in range(3):  # interim speech and pointing keep it open well past 0.5 s
        await asyncio.sleep(0.3)
        await band.send_json({"type": "transcript", "text": "and", "final": False})
        await asyncio.sleep(0.3)
        await app.send_json({"type": "app.interaction", "bugId": bug_id,
                             "items": [{"kind": "tap", "elementId": "list", "selected": True}]})
    assert (await (await client.get("/api/state")).json())["openBugId"] == bug_id
    a = await drain(app, 1.2)  # then silence
    assert types(a)[-2:] == ["bug.closing", "bug.submitted"]
    bug = await (await client.get(f"/api/bugs/{bug_id}")).json()
    assert bug["status"] == "submitted" and bug["closedBy"] == "idle"
    assert (await (await client.get("/api/state")).json())["session"] is not None


async def test_idle_empty_ticket_is_cancelled(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "ticket_idle": 30, "ticket_empty_idle": 0.4}))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    bug_id = (await drain(app))[0]["bugId"]
    a = await drain(app, 1.0)
    assert types(a) == ["bug.cancelled"] and a[0]["reason"] == "empty"
    bug = await (await client.get(f"/api/bugs/{bug_id}")).json()
    assert bug["status"] == "cancelled" and bug["closedBy"] == "idle"
    assert "bug.submitted" not in types(await drain(agent))

    await band.send_json({"type": "gesture", "name": "single"})  # with content, the longer limit applies
    await band.send_json({"type": "transcript", "text": "now with words", "final": True})
    await drain(app, 1.0)
    assert (await (await client.get("/api/state")).json())["openBugId"] is not None


async def test_restart_mid_session_keeps_session_and_open_ticket(aiohttp_client, paths):
    first = await aiohttp_client(create_app(**paths))
    app, band = await connect(first, "app"), await connect(first, "band")
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "transcript", "text": "before the restart", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "gesture", "name": "single"})
    msgs = await drain(app, sessions=True)
    session_id = msgs[0]["sessionId"]
    open_id = [m for m in msgs if m["type"] == "bug.opened"][-1]["bugId"]
    await first.close()

    second = await aiohttp_client(create_app(**{**paths, "ticket_idle": 0.4}))
    app = await connect(second, "app")
    state = app.hello["state"]
    assert state["session"]["id"] == session_id and state["openBugId"] == open_id
    assert [b["sessionId"] for b in state["bugs"]] == [session_id, session_id]
    await post(second, "/api/debug/transcript", {"text": "after the restart", "final": True})
    a = await drain(app, 1.2)  # the idle watcher was resumed for the open ticket
    assert types(a)[-2:] == ["bug.closing", "bug.submitted"]
    status, body = await post(second, "/api/debug/gesture", {"name": "double"})
    assert body["action"] == "session.end"
    state = await (await second.get("/api/state")).json()
    assert state["session"] is None and state["sessions"][0]["tickets"] == 2


async def test_next_settles_the_open_ticket_and_opens_another(client):
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]

    await band.send_json({"type": "gesture", "name": "next"})  # no session: starts one and opens a ticket
    a = await drain(app, sessions=True)
    assert [m["type"] for m in a] == ["session.started", "bug.opened"]
    first, session_id = a[1]["bugId"], a[0]["sessionId"]

    await band.send_json({"type": "transcript", "text": "The total is wrong. Next issue.", "final": True})
    await band.send_json({"type": "transcript", "text": "next issue", "final": False})
    await drain(app)
    await band.send_json({"type": "gesture", "name": "next"})
    a = await drain(app, 0.8, sessions=True)
    assert [m["type"] for m in a] == ["bug.closing", "bug.submitted", "bug.opened"]  # same session, no new one
    second = a[2]["bugId"]
    handed = [m for m in await drain(agent) if m["type"] == "bug.submitted"][0]["bug"]
    assert [t["text"] for t in handed["transcript"]] == ["The total is wrong."]  # the command is stripped
    assert handed["closedBy"] == "next" and handed["sessionId"] == session_id

    # the command itself arriving late as a final is not stored in the new ticket; real speech is
    await band.send_json({"type": "transcript", "text": "Next issue.", "final": True})
    await band.send_json({"type": "transcript", "text": "the logo is blurry", "final": True})
    await drain(app)
    bug = await (await client.get(f"/api/bugs/{second}")).json()
    assert bug["sessionId"] == session_id and [t["text"] for t in bug["transcript"]] == ["the logo is blurry"]

    # a final that lands inside the short window still belongs to the ticket being closed
    await band.send_json({"type": "gesture", "name": "next"})
    await band.send_json({"type": "transcript", "text": "on the home screen, next bug", "final": True})
    a = await drain(app, 0.8)
    assert types(a) == ["bug.closing", "transcript", "bug.submitted", "bug.opened"]
    bug = await (await client.get(f"/api/bugs/{second}")).json()
    assert [t["text"] for t in bug["transcript"]] == ["the logo is blurry", "on the home screen"]
    third = a[3]["bugId"]

    # nothing said in the third: "next" cancels it as empty, the agent never sees it, a fourth opens
    await drain(agent)
    await band.send_json({"type": "transcript", "text": "next feedback", "final": True})
    await band.send_json({"type": "gesture", "name": "next"})
    a = await drain(app, 0.8)
    assert types(a) == ["transcript", "bug.closing", "bug.cancelled", "bug.opened"] and a[2]["reason"] == "empty"
    assert "bug.submitted" not in types(await drain(agent))
    assert (await (await client.get(f"/api/bugs/{third}")).json())["transcript"] == []

    state = await (await client.get("/api/state")).json()
    assert state["session"]["id"] == session_id and state["openBugId"] == a[3]["bugId"]


async def test_next_with_a_session_but_no_ticket_and_the_debug_route(client):
    app = await connect(client, "app")
    await post(client, "/api/debug/gesture", {"name": "double"})
    status, body = await post(client, "/api/debug/gesture", {"name": "next"})
    assert status == 200 and body["action"] == "ticket.next" and body["bug"]["status"] == "open"
    assert types(await drain(app)) == ["bug.opened"]
    await post(client, "/api/debug/transcript", {"text": "first thing", "final": True})
    status, body = await post(client, "/api/debug/gesture", {"name": "next"})  # answers once the old one settled
    assert body["bug"]["status"] == "submitted" and body["openBugId"] not in (None, body["bug"]["id"])


async def test_app_diag_is_logged_not_fanned_out(client, paths):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await drain(dash)
    await app.send_json({"type": "app.diag", "items": [{"ev": "touchstart", "x": 10}, "plain text", {"big": "x" * 9000}]})
    await app.send_json({"type": "app.diag", "items": "nope"})
    await app.send_json({"type": "app.diag", "items": [{"n": i} for i in range(500)]})  # capped per message
    await band.send_json({"type": "app.diag", "items": [{"ev": "from the wrong role"}]})
    assert await drain(dash) == [] and await drain(band) == []
    lines = (await (await client.get("/api/debug/diag?n=5000")).json())["lines"]
    assert len(lines) == 3 + 200
    assert lines[0]["item"] == {"ev": "touchstart", "x": 10} and lines[1]["item"] == "plain text"
    assert lines[0]["client"].startswith("app_") and isinstance(lines[0]["at"], int)
    assert len(lines[2]["item"]["truncated"]) == 4000
    assert (paths["data_dir"] / "diag.log").read_text().count("\n") == 203
    last = (await (await client.get("/api/debug/diag?n=2")).json())["lines"]
    assert [x["item"] for x in last] == [{"n": 198}, {"n": 199}]
    assert (await (await client.get("/api/debug/diag?n=abc")).json())["lines"]


# ----------------------------------------------------------------- closing waits for speech to finish


def timed(paths, **timing):
    return create_app(**{**paths, "submit_grace": 0.1, "timing": {**paths["timing"], **timing}})


async def open_ticket(band, app):
    await band.send_json({"type": "gesture", "name": "double"})
    await band.send_json({"type": "gesture", "name": "single"})
    return [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]


async def test_closing_waits_while_the_band_says_speech_is_active(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, speech_settle=0.2, close_cap=5.0))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    bug_id = await open_ticket(band, app)
    await band.send_json({"type": "speech", "active": True})
    await band.send_json({"type": "transcript", "text": "I want balance numbers", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})  # rotated mid-sentence
    a = await drain(app, 0.6)  # far longer than the old fixed window: still closing, the person is talking
    assert types(a) == ["transcript", "bug.closing"]
    assert await drain(agent) == []
    await band.send_json({"type": "transcript", "text": "to be red", "final": True})  # the rest, seconds later
    assert types(await drain(app, 0.3)) == ["transcript"]  # a final alone does not end it while speech is active
    await band.send_json({"type": "speech", "active": False})
    assert await drain(app, 0.1) == []  # settles speech_settle (0.2 s) after speech stopped, not at once
    assert types(await drain(app, 0.4)) == ["bug.submitted"]
    handed = (await drain(agent))[0]["bug"]
    assert [t["text"] for t in handed["transcript"]] == ["I want balance numbers", "to be red"]
    assert handed["id"] == bug_id


async def test_flushed_ends_the_window_at_once_and_the_cap_bounds_it(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, speech_settle=5.0, close_cap=0.6))
    app, band = await connect(client, "app"), await connect(client, "band")
    await open_ticket(band, app)
    await band.send_json({"type": "speech", "active": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "transcript", "text": "make the title bigger", "final": True})
    await band.send_json({"type": "speech", "active": False, "flushed": True})  # nothing more is coming
    a = await drain(app, 0.3)
    assert types(a) == ["bug.closing", "transcript", "bug.submitted"]  # no 5 s settle wait

    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "speech", "active": True})  # a stuck "active" cannot hold a ticket forever
    await band.send_json({"type": "transcript", "text": "still going", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    assert types(await drain(app, 0.3)) == ["bug.opened", "transcript", "bug.closing"]
    assert types(await drain(app, 0.6)) == ["bug.submitted"]  # the 0.6 s cap


async def test_without_a_speech_signal_recent_transcripts_extend_the_window(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, quiet_window=0.5, speech_settle=0.15))
    app, band = await connect(client, "app"), await connect(client, "band")
    await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "the chart", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    for _ in range(4):  # interims keep arriving: the sentence is not over (0.8 s, past grace and quiet window)
        await asyncio.sleep(0.2)
        await band.send_json({"type": "transcript", "text": "has no", "final": False})
    assert "bug.submitted" not in types(await drain(app, 0.05))
    await band.send_json({"type": "transcript", "text": "has no legend", "final": True})  # the final after the last interim
    assert types(await drain(app, 0.05)) == ["transcript"]
    assert types(await drain(app, 0.4)) == ["bug.submitted"]  # speech_settle later, not a full quiet window
    bug = (await (await client.get("/api/bugs")).json())[0]
    assert [t["text"] for t in bug["transcript"]] == ["the chart", "has no legend"]

    await band.send_json({"type": "gesture", "name": "single"})  # silent person: grace, then quiet window
    await band.send_json({"type": "transcript", "text": "x", "final": True})
    await asyncio.sleep(0.6)
    await drain(app)
    await band.send_json({"type": "gesture", "name": "single"})
    assert types(await drain(app, 0.3)) == ["bug.closing", "bug.submitted"]


async def test_next_and_idle_also_wait_for_speech(aiohttp_client, paths):
    client = await aiohttp_client(create_app(**{**paths, "ticket_idle": 0.3, "timing": {
        **paths["timing"], "speech_settle": 0.3, "next_grace": 0.1, "next_cap": 0.8}}))
    app, band = await connect(client, "app"), await connect(client, "band")
    first = await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "first thing", "final": True})
    await band.send_json({"type": "speech", "active": True})
    assert types(await drain(app, 0.7)) == ["transcript"]  # idle limit is 0.3 s, but the person is still talking
    await band.send_json({"type": "gesture", "name": "next"})
    assert types(await drain(app, 0.3)) == ["bug.closing"]  # "next" waits too (its own short settle is 0.1 s)
    await band.send_json({"type": "transcript", "text": "and one more detail next issue", "final": True})
    await band.send_json({"type": "speech", "active": False})
    assert types(await drain(app, 0.25)) == ["transcript", "bug.submitted", "bug.opened"]
    bug = await (await client.get(f"/api/bugs/{first}")).json()
    assert [t["text"] for t in bug["transcript"]] == ["first thing", "and one more detail"]

    await band.send_json({"type": "speech", "active": True})  # stuck active again: "next" is capped at 0.8 s
    await band.send_json({"type": "transcript", "text": "second thing", "final": True})
    await band.send_json({"type": "gesture", "name": "next"})
    assert "bug.submitted" not in types(await drain(app, 0.5))
    assert types(await drain(app, 0.6)) == ["bug.submitted", "bug.opened"]


async def test_late_final_amends_the_submitted_ticket(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, late_final=1.0))
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]
    bug_id = await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "I want balance numbers", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await drain(app, 0.3)
    assert types(await drain(agent)) == ["bug.submitted"]
    for ws in (dash, band):
        await drain(ws)

    await band.send_json({"type": "transcript", "text": "to be red", "final": True})  # after the verdict
    a = await drain(agent)
    assert types(a) == ["bug.submitted"] and a[0]["amended"] is True and a[0]["bugId"] == bug_id
    assert [(t["text"], t.get("amended", False)) for t in a[0]["bug"]["transcript"]] == [
        ("I want balance numbers", False), ("to be red", True)]
    b = await drain(band)
    assert types(b) == ["bug.submitted"] and b[0]["amended"] is True  # the band can write it up again
    d = await drain(dash)
    assert types(d) == ["transcript", "bug.upsert"] and d[0]["bugId"] == bug_id and d[1]["bug"]["amended"] is True
    assert types(await drain(app)) == ["transcript"]  # the phone is not told again

    # once the agent is done, later words no longer change the ticket
    await agent.send_json({"type": "bug.fix", "bugId": bug_id, "state": "done", "summary": "ok"})
    await band.send_json({"type": "transcript", "text": "and bold", "final": True})
    await drain(app)
    assert len((await (await client.get(f"/api/bugs/{bug_id}")).json())["transcript"]) == 2

    # outside the window: just speech
    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "transcript", "text": "another one", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await asyncio.sleep(1.3)
    await drain(agent)
    await band.send_json({"type": "transcript", "text": "much later", "final": True})
    assert await drain(agent) == []


async def test_late_final_routing_with_a_new_ticket_open(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, late_final=2.0))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    first = await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "first ticket", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await drain(app, 0.3)
    await band.send_json({"type": "gesture", "name": "single"})
    second = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
    await drain(agent)

    await band.send_json({"type": "transcript", "text": "unmarked words", "final": True})  # to the open ticket
    await band.send_json({"type": "transcript", "text": "tail of the first", "final": True, "bugId": first})
    await band.send_json({"type": "transcript", "text": "marked for the second", "final": True, "bugId": second})
    a = await drain(agent)
    assert types(a) == ["bug.submitted"] and a[0]["amended"] is True and a[0]["bugId"] == first
    one = await (await client.get(f"/api/bugs/{first}")).json()
    two = await (await client.get(f"/api/bugs/{second}")).json()
    assert [t["text"] for t in one["transcript"]] == ["first ticket", "tail of the first"]
    assert [t["text"] for t in two["transcript"]] == ["unmarked words", "marked for the second"]
    assert two["status"] == "open" and "amended" not in two

    # a band that reports speech marks late words itself: unmarked speech right after a submit is not an amendment
    await band.send_json({"type": "gesture", "name": "single"})
    await drain(app, 0.3)
    await drain(agent)
    await band.send_json({"type": "speech", "active": False})
    await band.send_json({"type": "transcript", "text": "where is the pay button", "final": True})
    assert await drain(agent) == []
    await band.send_json({"type": "transcript", "text": "and marked", "final": True, "bugId": second})
    assert [m.get("amended") for m in await drain(agent)] == [True]


async def test_late_final_revives_a_ticket_closed_as_empty_but_not_a_cancelled_one(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, late_final=1.0))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    bug_id = await open_ticket(band, app)
    await band.send_json({"type": "gesture", "name": "single"})  # nothing arrived yet: closed as empty
    assert types(await drain(app, 0.3)) == ["bug.closing", "bug.cancelled"]
    await band.send_json({"type": "transcript", "text": "the whole sentence came late", "final": True})
    a = await drain(app)
    assert types(a) == ["transcript", "bug.submitted"] and a[1]["amended"] is True
    handed = [m for m in await drain(agent) if m["type"] == "bug.submitted"]
    assert len(handed) == 1 and handed[0]["bug"]["status"] == "submitted" and "cancelReason" not in handed[0]["bug"]

    await band.send_json({"type": "gesture", "name": "single"})
    await band.send_json({"type": "transcript", "text": "never mind", "final": True})
    await band.send_json({"type": "gesture", "name": "cancel"})
    await drain(app)
    await drain(agent)
    await band.send_json({"type": "transcript", "text": "said after cancelling", "final": True})
    assert await drain(agent) == []
    assert (await (await client.get("/api/bugs")).json())[0]["status"] == "cancelled"
    assert bug_id


# ----------------------------------------------------------------- flow review fixes


async def test_nothing_reopens_a_session_that_is_ending(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, speech_settle=0.4, close_cap=2.0))
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await open_ticket(band, app)
    await band.send_json({"type": "speech", "active": True})
    await band.send_json({"type": "transcript", "text": "the footer is cut off", "final": True})
    await drain(app)
    await drain(dash)
    await band.send_json({"type": "gesture", "name": "double"})  # end the session; the ticket starts settling
    for name in ("single", "next", "double", "open"):  # all arrive inside the closing window
        await band.send_json({"type": "gesture", "name": name})
    d = [m for m in await drain(dash, 0.2) if m["type"] == "gesture"]
    assert [(m["name"], m["accepted"]) for m in d] == [("double", True), ("single", False), ("next", False),
                                                        ("double", False), ("open", False)]
    await band.send_json({"type": "speech", "active": False})
    a = await drain(app, 0.8, sessions=True)
    assert [m["type"] for m in a] == ["bug.closing", "bug.submitted", "session.ended"]  # no new session, no new ticket
    state = await (await client.get("/api/state")).json()
    assert state["session"] is None and state["openBugId"] is None and len(state["bugs"]) == 1

    await band.send_json({"type": "gesture", "name": "single"})  # and afterwards a single is ignored as usual
    assert await drain(app, sessions=True) == []
    await band.send_json({"type": "gesture", "name": "double"})  # while a double starts a fresh session
    assert [m["type"] for m in await drain(app, sessions=True)] == ["session.started"]


async def test_single_during_a_next_window_is_reported_as_accepted(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, next_grace=0.5, next_cap=1.0))
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    first = await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "one", "final": True})
    await drain(dash)
    await band.send_json({"type": "gesture", "name": "next"})
    await band.send_json({"type": "gesture", "name": "single"})
    d = [m for m in await drain(dash) if m["type"] == "gesture"]
    assert [(m["name"], m["accepted"]) for m in d] == [("next", True), ("single", True)]
    opened = [m["bugId"] for m in await drain(app) if m["type"] == "bug.opened"]
    assert len(opened) == 1 and opened[0] != first  # exactly one new ticket


async def test_filler_and_short_late_words_do_not_amend_or_revive(aiohttp_client, paths):
    client = await aiohttp_client(timed(paths, late_final=2.0))
    app, band, agent = [await connect(client, r) for r in ("app", "band", "agent")]
    sent = await open_ticket(band, app)
    await band.send_json({"type": "transcript", "text": "the icon is misaligned", "final": True})
    await band.send_json({"type": "gesture", "name": "single"})
    await drain(app, 0.3)
    await drain(agent)
    for text in ("Okay.", "Thanks", "yeah okay thanks"):  # bare filler, marked or not
        await band.send_json({"type": "transcript", "text": text, "final": True, "bugId": sent})
    assert await drain(agent) == []
    await band.send_json({"type": "transcript", "text": "by two pixels", "final": True, "bugId": sent})  # short but real
    assert [m.get("amended") for m in await drain(agent)] == [True]

    await band.send_json({"type": "gesture", "name": "single"})
    empty = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
    await band.send_json({"type": "gesture", "name": "single"})  # closed as empty
    assert types(await drain(app, 0.3))[-1] == "bug.cancelled"
    assert types(await drain(agent)) == ["bug.cancelled"]
    for text in ("Okay.", "the card", "where is"):  # filler or under three words: stays closed
        await band.send_json({"type": "transcript", "text": text, "final": True, "bugId": empty})
    assert await drain(agent) == []
    assert (await (await client.get(f"/api/bugs/{empty}")).json())["status"] == "cancelled"
    await band.send_json({"type": "transcript", "text": "the card number is unreadable", "final": True, "bugId": empty})
    assert [m.get("amended") for m in await drain(agent)] == [True]
    assert (await (await client.get(f"/api/bugs/{empty}")).json())["status"] == "submitted"


async def test_a_second_band_client_cannot_hide_the_real_band(client):
    dash, real = await connect(client, "dashboard"), await connect(client, "band")
    await real.send_json({"type": "band.state", "connected": True, "listening": True, "battery": 14,
                          "transport": "ble", "stt": "reconnecting"})
    d = await drain(dash)
    assert d[-1]["connected"] is True and d[-1]["stt"] == "reconnecting" and d[-1]["battery"] == 14

    tester = await connect(client, "band")  # e.g. `--no-band --say ...` while the real band runs
    await tester.send_json({"type": "band.state", "connected": False, "listening": False, "transport": "none"})
    await post(client, "/api/debug/band", {"connected": False, "transport": "ble"})  # even dressed up as hardware
    for msg in [m for m in await drain(dash) if m["type"] == "band.state"]:
        assert msg["connected"] is True and msg["transport"] == "ble"
    await tester.send_json({"type": "band.state", "connected": True, "listening": True, "battery": 99, "transport": "sim"})
    assert [m for m in await drain(dash) if m["type"] == "band.state"][-1]["battery"] == 14  # still the hardware one
    await tester.close()
    assert not [m for m in await drain(dash, 0.4) if m["type"] == "band.state" and not m["connected"]]
    assert (await (await client.get("/api/state")).json())["band"]["transport"] == "ble"

    await real.send_json({"type": "band.state", "connected": False, "listening": False, "transport": "ble"})
    assert [m for m in await drain(dash) if m["type"] == "band.state"][-1]["connected"] is False  # the real band may say so
    await real.close()
    await drain(dash, 0.4)
    state = await (await client.get("/api/state")).json()
    assert state["band"]["connected"] is False and state["clients"]["band"] == 0


async def test_band_connecting_late_gets_tickets_that_have_no_write_up(client):
    app = await connect(client, "app")
    await post(client, "/api/debug/gesture", {"name": "open"})
    await post(client, "/api/debug/transcript", {"text": "sent while no band was connected", "final": True})
    status, body = await post(client, "/api/debug/gesture", {"name": "submit"})
    done = body["bug"]["id"]
    await post(client, "/api/debug/gesture", {"name": "open"})
    await post(client, "/api/debug/transcript", {"text": "already written up", "final": True})
    status, body = await post(client, "/api/debug/gesture", {"name": "submit"})
    await post(client, "/api/debug/enrich", {"bugId": body["bug"]["id"], "title": "Has a title"})
    await drain(app)

    band = await client.ws_connect("/ws?role=band")
    msgs = [await band.receive_json(timeout=1), await band.receive_json(timeout=1)]
    assert [m["type"] for m in msgs] == ["hello", "bug.submitted"]
    assert msgs[1]["bugId"] == done and msgs[1]["catchup"] is True and msgs[1]["bug"]["transcript"]
    assert await drain(band) == []  # only the one without a write-up


async def test_a_client_that_stops_reading_is_dropped_and_others_keep_working(client, monkeypatch):
    import hub.server as server
    monkeypatch.setattr(server, "SEND_TIMEOUT_S", 0.2)
    hub = client.server.app[server.HUB]
    dash, band = await connect(client, "dashboard"), await connect(client, "band")

    class Stalled:  # a phone asleep with its socket open: sends never complete
        closed = False

        async def send_str(self, text):
            await asyncio.sleep(60)

        async def close(self, **kw):
            self.closed = True

    stalled = Stalled()
    hub.clients["app"].add(stalled)
    await drain(dash)
    await band.send_json({"type": "gesture", "name": "double"})
    started = asyncio.get_event_loop().time()
    d = await drain(dash, 0.6, sessions=True)
    assert "session.started" in [m["type"] for m in d]
    assert stalled not in hub.clients["app"] and stalled.closed is True
    await band.send_json({"type": "gesture", "name": "single"})  # the next message is not delayed again
    again = asyncio.get_event_loop().time()
    assert [m for m in await drain(dash) if m["type"] == "bug.upsert"]
    assert asyncio.get_event_loop().time() - again < 0.5 and again - started < 1.5


async def test_client_that_vanishes_during_hello_is_not_left_registered(client, monkeypatch):
    import hub.server as server
    hub = client.server.app[server.HUB]
    real_state = hub.state
    monkeypatch.setattr(hub, "state", lambda: (_ for _ in ()).throw(RuntimeError("gone during hello")))
    ws = await client.ws_connect("/ws?role=band")
    await ws.receive(timeout=1)  # the server side fails and closes
    monkeypatch.setattr(hub, "state", real_state)
    await asyncio.sleep(0.1)
    assert (await (await client.get("/api/state")).json())["clients"]["band"] == 0


async def test_band_control_reaches_only_the_hardware_band_and_ends_the_session_first(client):
    app, dash = await connect(client, "app"), await connect(client, "dashboard")
    status, body = await post(client, "/api/band/control", {"action": "connect"})
    assert status == 409 and body["error"] == "Band process not running"
    status, body = await post(client, "/api/band/control", {"action": "explode"})
    assert status == 400

    tester, real = await connect(client, "band"), await connect(client, "band")
    await tester.send_json({"type": "band.state", "connected": False, "transport": "none"})
    await drain(tester)
    status, body = await post(client, "/api/band/control", {"action": "connect"})
    assert status == 409  # a typed test client is not a band

    await real.send_json({"type": "band.state", "connected": False, "transport": "ble", "link": "searching",
                          "hint": "Double-tap the band so it blinks blue"})
    d = [m for m in await drain(dash) if m["type"] == "band.state"][-1]
    assert d["link"] == "searching" and d["hint"].startswith("Double-tap") and d["connected"] is False
    await drain(real)
    status, body = await post(client, "/api/band/control", {"action": "connect"})
    assert status == 200 and body["sent"] == 1
    got = await drain(real)
    assert types(got) == ["band.control"] and got[0]["action"] == "connect"
    assert await drain(tester) == []

    # disconnect during a session with a ticket open: the ticket is settled and the session ended first
    await real.send_json({"type": "band.state", "connected": True, "listening": True, "transport": "ble", "link": "connected"})
    await real.send_json({"type": "gesture", "name": "double"})
    await real.send_json({"type": "gesture", "name": "single"})
    await real.send_json({"type": "transcript", "text": "the banner is too tall", "final": True})
    await drain(real)
    await drain(app, sessions=True)
    status, body = await post(client, "/api/band/control", {"action": "disconnect"})
    assert status == 200 and body["sessionEnded"] is True
    got = [m["type"] for m in await drain(real, sessions=True)]
    assert got == ["bug.closing", "bug.submitted", "session.ended", "band.control"]  # control comes last
    assert [m["type"] for m in await drain(app, sessions=True)] == ["bug.closing", "bug.submitted", "session.ended"]
    state = await (await client.get("/api/state")).json()
    assert state["session"] is None and state["bugs"][0]["status"] == "submitted"


# ----------------------------------------------------------------- band, context, assist


async def test_band_state_context_and_disconnect(client):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await drain(dash)
    await band.send_json({"type": "band.state", "connected": True, "listening": True,
                          "battery": 82, "transport": "ble"})
    expected = {"connected": True, "listening": True, "battery": 82, "transport": "ble"}
    for ws in (app, dash):
        msgs = await drain(ws)
        assert types(msgs) == ["band.state"]
        assert {k: msgs[0][k] for k in expected} == expected

    await app.send_json({"type": "app.context", "screen": "home", "visibleElementIds": ["a", 5, "b"]})
    for ws in (dash, band):
        msgs = await drain(ws)
        assert types(msgs) == ["app.context"]
        assert msgs[0]["screen"] == "home" and msgs[0]["visibleElementIds"] == ["a", "b"]
    assert await drain(app) == []

    late = await connect(client, "dashboard")  # a reconnect gets the current state
    assert late.hello["state"]["band"] == expected
    assert late.hello["state"]["appContext"]["screen"] == "home"
    assert late.hello["state"]["clients"] == {"app": 1, "dashboard": 2, "band": 1, "agent": 0}

    await band.close()
    msgs = await drain(dash, 0.5)
    assert [m for m in msgs if m["type"] == "band.state"][0]["connected"] is False
    assert [m for m in msgs if m["type"] == "clients"][-1]["clients"]["band"] == 0


async def test_assist_flow(client):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await drain(dash)
    await band.send_json({"type": "transcript", "text": "Where do I freeze my card?", "final": True})
    await band.send_json({"type": "assist", "elementId": "card-block-toggle", "say": "It is on the Cards screen."})
    d = [m for m in await drain(dash) if m["type"] == "assist"]
    assert d[0]["elementId"] == "card-block-toggle" and d[0]["question"] == "Where do I freeze my card?"
    a = [m for m in await drain(app) if m["type"] == "assist"]
    assert a[0]["elementId"] == "card-block-toggle" and a[0]["say"] == "It is on the Cards screen."

    await app.send_json({"type": "app.assist.shown", "elementId": "card-block-toggle", "ok": True})
    d = await drain(dash)
    assert types(d) == ["app.assist.shown"] and d[0]["ok"] is True and d[0]["assistId"] == a[0]["assistId"]

    await band.send_json({"type": "assist.none", "say": "I could not find that."})
    assert types(await drain(dash)) == ["assist.none"] and types(await drain(app)) == ["assist.none"]

    assists = (await (await client.get("/api/state")).json())["assists"]
    assert [x["elementId"] for x in assists] == [None, "card-block-toggle"]  # newest first
    assert assists[1]["shown"] is True


# ----------------------------------------------------------------- interactions


async def test_interactions_are_stored_per_bug(client):
    app, dash, band = [await connect(client, r) for r in ("app", "dashboard", "band")]
    await band.send_json({"type": "gesture", "name": "open"})
    bug_id = (await drain(app))[0]["bugId"]
    await drain(dash)

    circle = {"at": 5, "kind": "circle", "screen": "cards", "elementIds": ["card-block-toggle", "card-limit"],
              "path": [[0.1, 0.2], [0.6, 0.2], [0.6, 0.5], [0.1, 0.2]]}
    await app.send_json({"type": "app.interaction", "bugId": bug_id, "items": [
        {"at": 1, "kind": "screen", "screen": "cards"},
        {"at": 2, "kind": "tap", "screen": "cards", "elementId": "card-block-toggle", "label": "Block card",
         "rect": {"x": 300, "y": 400, "w": 52, "h": 30}},
        circle, {"kind": "wave"}, "junk"]})
    d = await drain(dash)
    assert types(d) == ["bug.upsert"]
    bug = d[0]["bug"]
    assert [i["kind"] for i in bug["interactions"]] == ["screen", "tap", "circle"]
    assert bug["interactions"][2] == circle
    assert bug["elementIds"] == ["card-block-toggle", "card-limit"]  # pointed-at elements are involved

    await band.send_json({"type": "gesture", "name": "submit"})
    await app.send_json({"type": "app.interaction", "bugId": bug_id,  # the last flush, after submit
                         "items": [{"kind": "tap", "screen": "cards", "elementId": "pay-button"}]})
    await app.send_json({"type": "app.interaction", "bugId": "bug_nope", "items": [{"kind": "tap"}]})
    await app.send_json({"type": "app.interaction", "bugId": bug_id, "items": "nope"})
    await drain(dash)
    stored = await (await client.get(f"/api/bugs/{bug_id}")).json()
    assert len(stored["interactions"]) == 4 and isinstance(stored["interactions"][3]["at"], int)
    assert stored["elementIds"] == ["card-block-toggle", "card-limit", "pay-button"]

    await band.send_json({"type": "gesture", "name": "open"})  # a second ticket starts clean
    other = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
    assert (await (await client.get(f"/api/bugs/{other}")).json())["interactions"] == []
    assert (await client.get("/api/bugs/bug_nope")).status == 404


# ----------------------------------------------------------------- coding agent


async def test_agent_gets_tickets_and_reports_fixes(client):
    app, dash, band, agent = [await connect(client, r) for r in ("app", "dashboard", "band", "agent")]
    assert agent.hello["state"]["clients"]["agent"] == 1
    await band.send_json({"type": "gesture", "name": "open"})
    await band.send_json({"type": "transcript", "text": "Make this button green", "final": True})
    assert await drain(agent) == []  # nothing until the ticket is submitted
    await band.send_json({"type": "gesture", "name": "submit"})
    a = await drain(agent)
    assert types(a) == ["bug.submitted"]
    bug = a[0]["bug"]
    assert bug["transcript"][0]["text"] == "Make this button green" and "interactions" in bug
    for ws in (app, dash, band):
        await drain(ws)

    await band.send_json({"type": "bug.enrich", "bugId": bug["id"], "kind": "improvement", "title": "Green button"})
    a = await drain(agent)
    assert types(a) == ["bug.upsert"] and a[0]["bug"]["kind"] == "improvement"
    await drain(dash)

    await agent.send_json({"type": "bug.fix", "bugId": bug["id"], "state": "working"})
    await agent.send_json({"type": "bug.fix", "bugId": bug["id"], "state": "done", "summary": "Button is green now",
                           "files": ["src/screens/Pay.tsx"], "ms": 8400})
    d = await drain(dash)
    assert types(d) == ["bug.upsert", "bug.upsert"]
    assert d[0]["bug"]["fix"]["state"] == "working"
    started = d[0]["bug"]["fix"]["startedAt"]
    assert isinstance(started, int) and d[0]["bug"]["fix"]["ms"] == 0
    assert d[1]["bug"]["fix"] == {"state": "done", "summary": "Button is green now",
                                  "files": ["src/screens/Pay.tsx"], "ms": 8400, "startedAt": started}
    a = await drain(app)
    assert types(a) == ["bug.fix", "bug.fix"]
    assert a[1]["bugId"] == bug["id"] and a[1]["state"] == "done" and a[1]["summary"] == "Button is green now"
    assert await drain(band) == []

    await agent.send_json({"type": "bug.fix", "bugId": bug["id"], "state": "exploded"})  # ignored
    await agent.send_json({"type": "bug.fix", "bugId": "bug_nope", "state": "done"})
    await app.send_json({"type": "bug.fix", "bugId": bug["id"], "state": "failed"})  # only the agent may
    await agent.send_json({"type": "gesture", "name": "open"})
    assert await drain(dash) == []
    status, body = await post(client, "/api/debug/fix", {"bugId": bug["id"], "state": "failed", "summary": "x"})
    assert status == 200 and body["bug"]["fix"]["state"] == "failed"
    await post(client, "/api/debug/fix", {"bugId": bug["id"], "state": "working"})  # a retry restarts the clock
    status, body = await post(client, "/api/debug/fix", {"bugId": bug["id"], "state": "done"})
    assert body["bug"]["fix"]["startedAt"] >= started and 0 <= body["bug"]["fix"]["ms"] < 5000  # hub times it
    status, body = await post(client, "/api/debug/fix", {"bugId": bug["id"], "state": "nope"})
    assert status == 400


# ----------------------------------------------------------------- debug routes


async def test_debug_routes_drive_a_whole_bug_without_a_band(client):
    app, dash = await connect(client, "app"), await connect(client, "dashboard")
    await drain(dash)

    status, body = await post(client, "/api/debug/gesture", {"name": "open"})
    assert status == 200 and body["accepted"] is True
    bug_id = body["bug"]["id"]
    status, body = await post(client, "/api/debug/gesture", {"name": "open"})
    assert status == 200 and body["accepted"] is False and body["openBugId"] == bug_id

    status, body = await post(client, "/api/debug/transcript", {"text": "The pay", "final": False})
    assert status == 200 and body["appended"] is False
    status, body = await post(client, "/api/debug/transcript", {"text": "The pay button is cut off", "final": True})
    assert status == 200 and body["appended"] is True and body["bugId"] == bug_id

    status, body = await post(client, "/api/debug/assist", {"elementId": "pay-button", "say": "Here it is."})
    assert status == 200 and body["assist"]["elementId"] == "pay-button"
    status, body = await post(client, "/api/debug/assist", {"say": "Nothing matches."})
    assert status == 200 and body["assist"]["elementId"] is None

    status, body = await post(client, "/api/debug/interaction", {"items": [{"kind": "tap", "elementId": "pay-button"}]})
    assert status == 200 and body["added"] == 1 and body["bug"]["id"] == bug_id  # defaults to the open ticket
    status, body = await post(client, "/api/debug/gesture", {"name": "submit"})
    assert status == 200 and body["bug"]["status"] == "submitted"
    status, body = await post(client, "/api/debug/enrich", {"bugId": bug_id, "title": "Pay button clipped",
                                                           "summary": "s", "severity": "medium"})
    assert status == 200 and body["bug"]["title"] == "Pay button clipped"

    assert types(await drain(app)) == ["bug.opened", "transcript", "transcript", "assist", "assist.none",
                                       "bug.closing", "bug.submitted"]
    assert types(await drain(dash)) == ["gesture", "bug.upsert", "gesture", "transcript", "transcript",
                                        "bug.upsert", "assist", "assist.none", "bug.upsert", "gesture",
                                        "bug.upsert", "bug.upsert", "bug.upsert"]
    bug = await (await client.get(f"/api/bugs/{bug_id}")).json()
    assert bug["severity"] == "medium" and len(bug["transcript"]) == 1

    status, body = await post(client, "/api/debug/reset", {})
    assert status == 200 and await (await client.get("/api/bugs")).json() == []
    assert types(await drain(dash)) == ["hello"]


async def test_debug_routes_reject_bad_input(client):
    for path, body in [
        ("/api/debug/gesture", {"name": "wave"}),
        ("/api/debug/gesture", {}),
        ("/api/debug/transcript", {"text": "   ", "final": True}),
        ("/api/debug/transcript", {"text": 5}),
        ("/api/debug/assist", {}),
        ("/api/debug/assist", {"elementId": 7}),
        ("/api/debug/enrich", {"bugId": "bug_nope", "title": "x"}),
        ("/api/debug/interaction", {"items": [{"kind": "tap"}]}),
        ("/api/debug/fix", {"bugId": "bug_nope", "state": "done"}),
    ]:
        status, data = await post(client, path, body)
        assert status == 400 and data["ok"] is False and data["error"], (path, body)
    resp = await client.post("/api/debug/gesture", data="{not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400 and (await resp.json())["ok"] is False
    resp = await client.post("/api/debug/gesture", json=["open"])
    assert resp.status == 400
    assert await (await client.get("/api/bugs")).json() == []


# ----------------------------------------------------------------- malformed socket input


async def test_malformed_socket_messages_are_ignored(client):
    band, dash = await connect(client, "band"), await connect(client, "dashboard")
    await drain(dash)
    await band.send_str("{not json")
    await band.send_str("[1, 2, 3]")
    await band.send_str('"gesture"')
    await band.send_bytes(b"\x00\x01")
    for msg in [{"no": "type"}, {"type": 7}, {"type": "made.up"}, {"type": "gesture"},
                {"type": "gesture", "name": ["open"]}, {"type": "transcript", "text": None, "final": True},
                {"type": "assist", "say": "no element"}, {"type": "bug.enrich", "bugId": {"a": 1}},
                {"type": "bug.enrich"}, {"type": "band.state", "battery": "full"}]:
        await band.send_json(msg)
    msgs = await drain(dash)
    assert types(msgs) == ["band.state"] and msgs[0]["battery"] is None and msgs[0]["connected"] is False
    await band.send_json({"type": "gesture", "name": "open"})  # the socket is still alive and working
    assert types(await drain(dash)) == ["gesture", "bug.upsert"]

    bug_id = (await (await client.get("/api/state")).json())["openBugId"]
    await band.send_json({"type": "bug.enrich", "bugId": bug_id, "severity": "catastrophic",
                          "elementIds": "nope", "title": 9, "kind": "feature", "steps": "nope"})
    bug = [m for m in await drain(dash) if m["type"] == "bug.upsert"][0]["bug"]
    assert bug["severity"] == "" and bug["title"] == "" and bug["elementIds"] == []
    assert bug["kind"] == "" and bug["steps"] == []


# ----------------------------------------------------------------- persistence


async def test_bugs_survive_a_restart(aiohttp_client, paths):
    first = await aiohttp_client(create_app(**paths))
    app, band = await connect(first, "app"), await connect(first, "band")
    await band.send_json({"type": "gesture", "name": "open"})
    done_id = (await drain(app))[0]["bugId"]
    await band.send_json({"type": "transcript", "text": "persist me", "final": True})
    await app.send_json({"type": "app.interaction", "bugId": done_id,
                         "items": [{"at": 1, "kind": "tap", "screen": "cards", "elementId": "card-block-toggle"}]})
    await band.send_json({"type": "gesture", "name": "submit"})
    await band.send_json({"type": "bug.enrich", "bugId": done_id, "title": "Kept", "severity": "low"})
    await band.send_json({"type": "gesture", "name": "open"})
    open_id = [m for m in await drain(app) if m["type"] == "bug.opened"][0]["bugId"]
    await first.close()

    second = await aiohttp_client(create_app(**paths))
    dash = await connect(second, "dashboard")
    state = dash.hello["state"]
    assert [b["id"] for b in state["bugs"]] == [open_id, done_id]  # newest first
    kept = state["bugs"][1]
    assert kept["title"] == "Kept" and kept["status"] == "submitted" and kept["transcript"][0]["text"] == "persist me"
    assert state["openBugId"] == open_id  # a bug opened moments ago is still open
    assert kept["interactions"] == [{"at": 1, "kind": "tap", "screen": "cards", "elementId": "card-block-toggle"}]

    await post(second, "/api/debug/transcript", {"text": "said after the restart", "final": True})
    status, body = await post(second, "/api/debug/gesture", {"name": "submit"})
    assert body["bug"]["id"] == open_id and body["bug"]["status"] == "submitted"


async def test_stale_open_bug_is_cancelled_and_corrupt_file_is_survived(aiohttp_client, paths):
    data = paths["data_dir"]
    data.mkdir()
    recent = int(__import__("time").time() * 1000)
    (data / "bugs.json").write_text(json.dumps({"bugs": [
        {"id": "bug_mid", "status": "open", "closing": True, "openedAt": recent,  # the hub died mid-submit
         "transcript": [{"text": "kept", "at": recent}]},
        {"id": "bug_old", "status": "open", "openedAt": 1000, "transcript": []}, "junk", {"no": "id"}]}))
    client = await aiohttp_client(create_app(**paths))
    state = await (await client.get("/api/state")).json()
    assert state["openBugId"] is None and state["bugs"][0]["status"] == "cancelled"
    assert state["bugs"][1]["status"] == "submitted" and "closing" not in state["bugs"][1]
    assert state["bugs"][0]["interactions"] == [] and state["bugs"][0]["fix"]["state"] == ""  # old files are upgraded
    status, body = await post(client, "/api/debug/gesture", {"name": "open"})
    assert body["accepted"] is True
    await client.close()

    (data / "bugs.json").write_text("{definitely not json")
    client = await aiohttp_client(create_app(**paths))
    assert await (await client.get("/api/bugs")).json() == []
    assert list(data.glob("bugs.corrupt-*.json"))


# ----------------------------------------------------------------- static + kb


async def test_static_hosting_and_kb(client, paths):
    resp = await client.get("/")
    assert resp.status == 503 and "app not built" in await resp.text()
    resp = await client.get("/api/kb")
    assert resp.status == 404 and "does not exist yet" in (await resp.json())["error"]
    assert await (await client.get("/api/kb?optional=1")).json() == []

    dist = paths["app_dist"]
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root>app</div>")
    (dist / "assets" / "main.js").write_text("let a = 1")
    paths["kb_path"].write_text(json.dumps([{"id": "card-block-toggle", "screen": "cards", "label": "Block card"}]))

    assert "app</div>" in await (await client.get("/")).text()
    assert "app</div>" in await (await client.get("/cards/detail")).text()  # SPA fallback
    assert await (await client.get("/assets/main.js")).text() == "let a = 1"
    assert (await client.get("/assets/missing.js")).status == 404  # a missing file is not the SPA
    assert (await client.get("/..%2F..%2Felements.json")).status in (403, 404)
    assert (await (await client.get("/api/kb")).json())[0]["id"] == "card-block-toggle"

    resp = await client.get("/dashboard", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/dashboard/"
    assert "dash" in await (await client.get("/dashboard/")).text()
    assert "console.log" in await (await client.get("/dashboard/app.js")).text()
    assert (await client.get("/dashboard/nope.js")).status == 404

    resp = await client.get("/api/nope")
    assert resp.status == 404 and (await resp.json())["ok"] is False  # never the SPA page
    assert (await client.get("/api/state")).headers["Access-Control-Allow-Origin"] == "*"
