"""Pineo hub: one aiohttp process that is the single source of truth.

WebSocket fan-out between the phone app, the dashboard and the band process,
the ticket state machine, interaction capture, the HTTP API and static hosting.
See docs/CONTRACT.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

from aiohttp import WSMsgType, web

log = logging.getLogger("pineo.hub")

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("app", "dashboard", "band", "agent")
GESTURES = ("double", "single", "next", "open", "submit", "cancel")
SEVERITIES = ("low", "medium", "high")
KINDS = ("bug", "improvement")
FIX_STATES = ("queued", "working", "done", "failed")
INTERACTION_KINDS = ("tap", "circle", "screen", "scroll", "input")
MAX_INTERACTIONS = 500
STALE_OPEN_MS = 10 * 60 * 1000  # an open bug older than this is cancelled on restart
QUESTION_WINDOW_MS = 20 * 1000  # a final transcript this recent counts as the assist's question
MAX_ASSISTS = 30
TICKET_IDLE_S = 5.0  # a ticket with content and nothing new for this long is submitted
TICKET_EMPTY_IDLE_S = 15.0  # an empty ticket idle for this long is cancelled
STALE_SESSION_MS = 3 * 60 * 60 * 1000  # a session older than this is ended on restart
MAX_SESSIONS = 50
NEXT_GRACE_S = 0.4  # "next issue": the sentence is clearly over, wait only this long for a late final
NEXT_PHRASE = re.compile(r"[\s,;:-]*\b(?:next|new)\s+(?:issue|bug|feedback|ticket|one)\b[\s.!?,]*$", re.I)
NEXT_ECHO_MS = 3000  # the spoken command may still arrive as a final just after the new ticket opened
DIAG_MAX_ITEMS = 200
DIAG_MAX_LINE = 4000
DIAG_MAX_BYTES = 5 * 1024 * 1024
CLOSE_CAP_S = 4.5  # a closing window never lasts longer than this
NEXT_CAP_S = 2.0  # ... or this, for "next"
SPEECH_SETTLE_S = 0.5  # close this long after speech stopped (or after the final that followed the last interim)
QUIET_WINDOW_S = 1.5  # no speech signal from the band: a transcript this recent keeps the window open
SPEECH_STUCK_S = 30.0  # "speech active" with no transcript for this long is treated as a stuck signal
SEND_TIMEOUT_S = 1.5  # a client that does not take a message for this long is dropped
HARDWARE_TRANSPORTS = ("ble", "serial")  # a band.state from the real wristband outranks test clients
CATCHUP_MS = 2 * 60 * 1000  # a band that (re)connects is handed tickets this recent that have no write-up yet
FILLER = {"ok", "okay", "thanks", "thank", "you", "yes", "yeah", "yep", "no", "nope", "right", "alright", "cool",
          "great", "good", "nice", "perfect", "done", "so", "um", "uh", "hmm", "well", "sure", "fine", "please"}
LATE_FINAL_S = 4.0  # a final this soon after a ticket closed is appended to it (an amendment)
SUBMIT_GRACE_S = 1.2  # after a submit gesture, how long to wait for the last spoken words
MAX_BODY = 8 * 1024 * 1024

HUB = web.AppKey("hub", object)
APP_DIST = web.AppKey("app_dist", Path)
DASHBOARD_DIR = web.AppKey("dashboard_dir", Path)
KB_PATH = web.AppKey("kb_path", Path)


def now_ms() -> int:
    return int(time.time() * 1000)


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _strs(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def pointed_ids(bug: dict) -> list[str]:
    """Elements the person ended up pointing at: selected by a tap (and not
    deselected afterwards) or inside a drawn circle, in the order first chosen."""
    chosen: list[str] = []
    for item in bug.get("interactions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "tap" and isinstance(item.get("elementId"), str) and item["elementId"]:
            if item.get("selected") is False:
                if item["elementId"] in chosen:
                    chosen.remove(item["elementId"])
            elif item["elementId"] not in chosen:
                chosen.append(item["elementId"])
        elif item.get("kind") == "circle":
            chosen += [i for i in _strs(item.get("elementIds")) if i not in chosen]
    return chosen


def has_context(bug: dict) -> bool:
    """A ticket is worth submitting only if something was said or pointed at.
    Taps that ended deselected, screen changes and scrolling do not count."""
    if any(_str(line.get("text")).strip() for line in bug.get("transcript") or [] if isinstance(line, dict)):
        return True
    if pointed_ids(bug):
        return True
    for item in bug.get("interactions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in ("circle", "input"):  # a loop around empty space still points somewhere
            return True
        if item.get("kind") == "tap" and not item.get("elementId") and item.get("selected") is not False:
            return True
    return False


def _new_fix() -> dict:
    return {"state": "", "summary": "", "files": [], "ms": 0}


# --------------------------------------------------------------------------- store


class Store:
    """Tickets on disk (bugs.json)."""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bugs_file = self.dir / "bugs.json"
        self.bugs: dict[str, dict] = {}  # insertion order: oldest first
        self.open_id: str | None = None
        self.session: dict | None = None  # {"id", "startedAt"} while a debugging session is live
        self.sessions: list[dict] = []  # ended sessions, newest first
        self._load()

    def _load(self) -> None:
        if not self.bugs_file.is_file():
            return
        try:
            raw = json.loads(self.bugs_file.read_text("utf-8"))
            bugs = raw["bugs"] if isinstance(raw, dict) else raw
            assert isinstance(bugs, list)
            if isinstance(raw, dict):
                session = raw.get("session")
                if isinstance(session, dict) and isinstance(session.get("id"), str):
                    self.session = {"id": session["id"], "startedAt": int(session.get("startedAt") or 0)}
                if isinstance(raw.get("sessions"), list):
                    self.sessions = [x for x in raw["sessions"] if isinstance(x, dict) and isinstance(x.get("id"), str)]
        except Exception as exc:  # corrupt file: keep it aside, start clean
            aside = self.bugs_file.with_name(f"bugs.corrupt-{now_ms()}.json")
            log.warning("could not read %s (%s); moved to %s", self.bugs_file, exc, aside.name)
            self.bugs_file.replace(aside)
            return
        now = now_ms()
        for bug in bugs:
            if not isinstance(bug, dict) or not isinstance(bug.get("id"), str):
                continue
            if bug.get("status") == "open" and bug.pop("closing", None):  # the hub stopped mid-submit
                self._settle(bug)
            if bug.get("status") == "open":
                if self.open_id is None and now - int(bug.get("openedAt") or 0) < STALE_OPEN_MS:
                    self.open_id = bug["id"]
                else:
                    bug["status"] = "cancelled"
            for key, default in (("kind", ""), ("interactions", []), ("steps", []), ("transcript", []),
                                 ("elementIds", []), ("fix", _new_fix())):
                if not isinstance(bug.get(key), type(default)):
                    bug[key] = default
            self.bugs[bug["id"]] = bug
        if self.session and self.open_id is None and now - self.session["startedAt"] > STALE_SESSION_MS:
            self.end_session()  # left over from an earlier day
        self.save()

    def start_session(self) -> dict:
        self.session = {"id": f"ses_{now_ms():x}{secrets.token_hex(2)}", "startedAt": now_ms()}
        self.save()
        return self.session

    def end_session(self) -> dict | None:
        if self.session is None:
            return None
        tickets = sum(1 for b in self.bugs.values()
                      if b.get("sessionId") == self.session["id"] and b.get("status") == "submitted")
        ended = {**self.session, "endedAt": now_ms(), "tickets": tickets}
        self.sessions.insert(0, ended)
        del self.sessions[MAX_SESSIONS:]
        self.session = None
        self.save()
        return ended

    def save(self) -> None:
        tmp = self.bugs_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"bugs": list(self.bugs.values()), "session": self.session,
                                   "sessions": self.sessions}, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.bugs_file)

    def list(self) -> list[dict]:
        return list(reversed(self.bugs.values()))  # newest first

    @property
    def open_bug(self) -> dict | None:
        return self.bugs.get(self.open_id) if self.open_id else None

    def open(self, context: dict | None) -> dict:
        context = context or {}
        bug = {
            "id": f"bug_{now_ms():x}{secrets.token_hex(3)}",
            "sessionId": self.session["id"] if self.session else None,
            "status": "open",
            "kind": "",
            "openedAt": now_ms(),
            "submittedAt": 0,
            "transcript": [],
            "interactions": [],
            "screen": _str(context.get("screen")),
            "elementIds": [],
            "contextAtOpen": context,
            "title": "",
            "summary": "",
            "severity": "",
            "steps": [],
            "fix": _new_fix(),
        }
        self.bugs[bug["id"]] = bug
        self.open_id = bug["id"]
        self.save()
        return bug

    @staticmethod
    def _settle(bug: dict) -> None:
        """Submit gesture outcome: a ticket with nothing captured is cancelled instead."""
        bug.pop("closing", None)
        bug["closedAt"] = now_ms()
        if has_context(bug):
            bug["status"] = "submitted"
            bug["submittedAt"] = now_ms()
        else:
            bug["status"] = "cancelled"
            bug["cancelReason"] = "empty"

    def close(self, status: str) -> dict | None:
        """status: "submitted" (decided by content) or "cancelled" (the person cancelled)."""
        bug = self.open_bug
        if bug is None:
            return None
        if status == "submitted":
            self._settle(bug)
        else:
            bug.pop("closing", None)
            bug["status"] = "cancelled"
            bug["cancelReason"] = "gesture"
            bug["closedAt"] = now_ms()
        self.open_id = None
        self.save()
        return bug

    def add_interactions(self, bug: dict, items: list) -> int:
        added = 0
        for item in items:
            if not isinstance(item, dict) or item.get("kind") not in INTERACTION_KINDS:
                continue
            if len(bug["interactions"]) >= MAX_INTERACTIONS:
                break
            if not isinstance(item.get("at"), (int, float)) or isinstance(item.get("at"), bool):
                item = {**item, "at": now_ms()}
            bug["interactions"].append(item)
            added += 1
        if added:
            if not bug.get("enriched"):  # until the write-up names them, the involved elements are the pointed-at ones
                bug["elementIds"] = pointed_ids(bug)
            self.save()
        return added

    def append_diag(self, client: str, items: list) -> int:
        """Phone diagnostics: one JSON line per item, rotated at about 5 MB."""
        path = self.dir / "diag.log"
        if path.is_file() and path.stat().st_size > DIAG_MAX_BYTES:
            os.replace(path, self.dir / "diag.log.1")
        lines = []
        for item in items[:DIAG_MAX_ITEMS]:
            text = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
            if len(text) > DIAG_MAX_LINE:
                item = {"truncated": text[:DIAG_MAX_LINE]}
            lines.append(json.dumps({"at": now_ms(), "client": client, "item": item}, ensure_ascii=False,
                                    separators=(",", ":"), default=str))
        if lines:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return len(lines)

    def read_diag(self, n: int) -> list:
        path = self.dir / "diag.log"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text("utf-8", errors="replace").splitlines()[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"raw": line})
        return out

    def reset(self) -> None:
        self.bugs.clear()
        self.open_id = None
        self.session = None
        self.sessions = []
        self.save()


# --------------------------------------------------------------------------- hub


class Hub:
    def __init__(self, store: Store, submit_grace: float = SUBMIT_GRACE_S,
                 ticket_idle: float = TICKET_IDLE_S, ticket_empty_idle: float = TICKET_EMPTY_IDLE_S):
        self.store = store
        self.submit_grace = submit_grace
        self.ticket_idle = ticket_idle
        self.ticket_empty_idle = ticket_empty_idle
        self.next_grace = NEXT_GRACE_S
        self.close_cap = CLOSE_CAP_S
        self.next_cap = NEXT_CAP_S
        self.speech_settle = SPEECH_SETTLE_S
        self.quiet_window = QUIET_WINDOW_S
        self.late_final = LATE_FINAL_S
        self.speech_seen = False  # the band sends `speech` messages (newer band)
        self.speech_active = False
        self.speech_at = 0.0  # monotonic time of the last speech state change
        self.tx_at = 0.0  # monotonic time of the last transcript, interim or final
        self.interim_pending = False  # the last transcript was an interim: a sentence is in flight
        self.last_closed_id: str | None = None  # most recently settled ticket, for late finals
        self.session_ending = False  # a double rotation arrived while a ticket was open
        self.open_after_close = False  # "next": open a new ticket as soon as the current one settles
        self._idle_at = time.monotonic()  # last speech or pointing on the open ticket
        self.closing: dict | None = None  # {"id", "event", "task"} while a submit waits for last words
        self.clients: dict[str, set[web.WebSocketResponse]] = {r: set() for r in ROLES}
        self.band_states: dict = {}  # per band connection (or "debug"): its last band.state
        self.app_context: dict | None = None
        self.assists: list[dict] = []  # newest first
        self.last_final: dict | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def band(self) -> dict:
        """The band status shown to everyone: the real wristband's if one reports (transport
        ble/serial), so a test client with transport "none"/"sim" cannot hide or clear it."""
        states = list(self.band_states.values())
        if not states:
            return self._band_default()
        return max(states, key=lambda st: (st["real"], st["connected"], st["_at"]))["public"]

    @staticmethod
    def _band_default() -> dict:
        return {"connected": False, "listening": False, "battery": None, "transport": None}

    def state(self) -> dict:
        return {
            "bugs": self.store.list(),
            "openBugId": self.store.open_id,
            "session": self.store.session,
            "sessions": self.store.sessions,
            "band": self.band,
            "appContext": self.app_context,
            "assists": self.assists,
            "clients": {r: len(self.clients[r]) for r in ROLES},
        }

    # ---- sending

    async def send(self, roles: Iterable[str], msg: dict) -> None:
        msg.setdefault("ts", now_ms())
        text = json.dumps(msg, ensure_ascii=False)
        targets = [(role, ws) for role in roles for ws in list(self.clients[role])]
        if not targets:
            return

        async def one(role: str, ws: web.WebSocketResponse) -> None:
            try:
                await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT_S)
            except asyncio.TimeoutError:  # not reading (a sleeping phone): drop it so it cannot stall the others again
                log.warning("dropping a stalled %s client", role)
                self.disconnect(role, ws)
                self._spawn(self._close_quietly(ws))
            except Exception:  # a dead client must not block the others
                pass

        await asyncio.gather(*(one(role, ws) for role, ws in targets))

    @staticmethod
    async def _close_quietly(ws: web.WebSocketResponse) -> None:
        try:
            await asyncio.wait_for(ws.close(code=1011, message=b"too slow"), 1)
        except Exception:
            pass

    async def upsert(self, bug: dict, agent: bool = False) -> None:
        await self.send(["dashboard", "agent"] if agent else ["dashboard"], {"type": "bug.upsert", "bug": bug})

    async def send_clients(self) -> None:
        counts = {r: len(self.clients[r]) for r in ROLES}
        await self.send(["dashboard"], {"type": "clients", "clients": counts})

    # ---- connections

    async def connect(self, role: str, ws: web.WebSocketResponse) -> None:
        self.clients[role].add(ws)
        await ws.send_str(json.dumps(
            {"type": "hello", "ts": now_ms(), "role": role, "state": self.state()}, ensure_ascii=False))
        if role == "band":  # tickets sent while no band was connected still need their write-up
            for bug in self.store.list():
                if bug["status"] == "submitted" and not bug.get("title") \
                        and now_ms() - int(bug.get("submittedAt") or 0) < CATCHUP_MS:
                    await ws.send_str(json.dumps({"type": "bug.submitted", "ts": now_ms(), "bugId": bug["id"],
                                                  "bug": bug, "catchup": True}, ensure_ascii=False))
        await self.send_clients()

    def disconnect(self, role: str, ws: web.WebSocketResponse) -> None:
        """Synchronous on purpose: aiohttp cancels the socket handler when the
        connection drops, so the state change must not wait on anything. The
        fan-out runs in its own task. Safe to call twice for the same socket."""
        if ws not in self.clients[role]:
            return
        self.clients[role].discard(ws)
        before = self.band
        if role == "band":
            self.band_states.pop(ws, None)  # its status goes with it; another band's status may take over
            if not self.clients["band"]:
                self.speech_seen = self.speech_active = False  # no band, no speech signal
        band_changed = self.band != before

        async def announce() -> None:
            if band_changed:
                await self.send(["app", "dashboard"], {"type": "band.state", **self.band})
            await self.send_clients()

        self._spawn(announce())

    # ---- inbound routing

    async def handle(self, role: str, msg: Any, ws: web.WebSocketResponse | None = None) -> None:
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            return
        if role == "band" and msg["type"] == "band.state":
            await self.on_band_state(msg, ws)  # kept per connection
            return
        handler = self.ROUTES.get((role, msg["type"]))
        if handler is not None:  # unknown types (or a type from the wrong role) are ignored
            await handler(self, msg)

    async def on_band_state(self, msg: dict, source: Any = "debug") -> dict:
        battery = msg.get("battery")
        public = {
            "connected": bool(msg.get("connected")),
            "listening": bool(msg.get("listening")),
            "battery": battery if isinstance(battery, (int, float)) and not isinstance(battery, bool) else None,
            "transport": msg.get("transport") if isinstance(msg.get("transport"), str) else None,
        }
        for key in ("stt", "link", "hint"):  # speech link; "connected|connecting|disconnected|searching"; what to do
            if isinstance(msg.get(key), str):
                public[key] = msg[key]
        # the real band process: a hardware transport, or it reports `link` (test clients never do)
        debug = source is None or source == "debug"  # the debug route can only stand in while no band reports
        if not debug:
            self.band_states.pop("debug", None)
        self.band_states["debug" if debug else source] = {
            "public": public, "connected": public["connected"], "transport": public["transport"],
            "real": not debug and (public["transport"] in HARDWARE_TRANSPORTS or "link" in public),
            "_at": time.monotonic()}
        await self.send(["app", "dashboard"], {"type": "band.state", **self.band})
        return {"ok": True, "band": self.band}

    async def band_control(self, msg: dict) -> dict:
        """Dashboard button: ask the band process (the one with real hardware) to connect or
        release the wristband. Before a disconnect the session is ended cleanly."""
        action = msg.get("action")
        if action not in ("connect", "disconnect"):
            return {"ok": False, "error": "action must be connect or disconnect"}
        def targets() -> list:
            return [ws for ws in self.clients["band"] if self.band_states.get(ws, {}).get("real")]
        if not targets():
            return {"ok": False, "status": 409, "error": "Band process not running"}
        ended = False
        if action == "disconnect" and self.store.session is not None:
            if not self.session_ending:
                await self.on_gesture({"name": "double"})  # settles an open ticket, then ends the session
            await self.wait_closed()
            ended = self.store.session is None
        text = json.dumps({"type": "band.control", "ts": now_ms(), "action": action})
        sent = 0
        for ws in targets():
            try:
                await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT_S)
                sent += 1
            except Exception:
                pass
        return {"ok": sent > 0, "sent": sent, "sessionEnded": ended,
                **({} if sent else {"status": 409, "error": "Band process not reachable"})}

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- sessions

    async def _start_session(self) -> dict:
        session = self.store.start_session()
        await self.send(ROLES, {"type": "session.started", "sessionId": session["id"], "startedAt": session["startedAt"]})
        return session

    async def _end_session(self) -> None:
        self.session_ending = False
        ended = self.store.end_session()
        if ended:
            await self.send(ROLES, {"type": "session.ended", "sessionId": ended["id"], "startedAt": ended["startedAt"],
                                    "endedAt": ended["endedAt"], "tickets": ended["tickets"]})

    # ---- gestures (v3 table: double = session start/end, single = open ticket / manual submit)

    async def _open_ticket(self, opened_by: str = "gesture") -> dict:
        if self.store.session is None:
            await self._start_session()  # opening with no session starts one
        bug = self.store.open(self.app_context)
        bug["openedBy"] = opened_by
        await self.send(["app", "band"], {"type": "bug.opened", "bugId": bug["id"]})
        self._idle_at = time.monotonic()
        self._spawn(self._idle_watch(bug["id"]))
        return bug

    async def on_gesture(self, msg: dict) -> dict:
        name = msg.get("name")
        if name not in GESTURES:
            return {"ok": False, "error": f"name must be one of {', '.join(GESTURES)}"}
        open_bug = self.store.open_bug
        if self.session_ending and name in ("double", "single", "next", "open"):
            # the session is ending (its last ticket is settling): nothing may reopen it or start another
            await self.send(["dashboard"], {"type": "gesture", "name": name, "action": None, "accepted": False,
                                            "closing": False, "bugId": self.store.open_id, "reason": "session ending"})
            return {"ok": True, "action": None, "accepted": False, "closing": False, "bug": open_bug,
                    "openBugId": self.store.open_id, "session": self.store.session}
        if name == "next":
            return await self._next(open_bug)
        if name == "double":
            action = "session.end" if self.store.session else "session.start"
        elif name == "single":
            if self.store.session is None:
                action = None  # a single rotation outside a session is ignored
            else:
                action = "ticket.submit" if open_bug is not None and not self.closing else "ticket.open"
        else:
            action = f"ticket.{name}"

        bug, closing, accepted = None, False, False
        if action == "session.start":
            await self._start_session()
            accepted = True
        elif action == "session.end":
            accepted = True
            if open_bug is None:
                await self._end_session()
            else:  # settle the open ticket first (closing window + empty rule), then end
                self.session_ending = True
                if not self.closing:
                    bug, closing = open_bug, True
                    bug["closedBy"] = "session-end"
        elif action == "ticket.open":
            chained = self.open_after_close  # a "next" is settling: it opens the new ticket itself
            if self.closing:  # a new report right after a submit: settle the old one now
                await self._finish_close(self.closing["id"])
            if chained and self.store.open_bug is not None:
                bug, accepted = self.store.open_bug, True
            elif self.store.open_id is None:  # a second open while one is open is ignored
                bug = await self._open_ticket()
                accepted = True
        elif action == "ticket.submit":
            if open_bug is not None and not self.closing:  # ignored with nothing open, or twice
                bug, closing, accepted = open_bug, True, True
                bug["closedBy"] = "gesture"
        elif action == "ticket.cancel":
            self.closing = None  # a pending submit is overridden; its timer finds nothing to do
            self.open_after_close = False
            bug = self.store.close("cancelled")
            accepted = bug is not None
            if bug is not None:
                self.last_closed_id = bug["id"]  # late words must not land on an older ticket

        gesture = {"type": "gesture", "name": name, "action": action, "accepted": accepted, "closing": closing,
                   "bugId": bug["id"] if bug else self.store.open_id}
        if isinstance(msg.get("confidence"), (int, float)):
            gesture["confidence"] = msg["confidence"]
        if closing:  # the phone hears first; the dashboard is told in order: gesture, then the ticket
            await self.send(["app", "band"], {"type": "bug.closing", "bugId": bug["id"]})
        await self.send(["dashboard"], gesture)
        if closing:
            await self._begin_close(bug)
        elif action == "ticket.cancel" and bug is not None:
            await self._announce_closed(bug)
        elif action == "ticket.open" and bug is not None:
            await self.upsert(bug)
        return {"ok": True, "action": action, "accepted": accepted, "closing": closing, "bug": bug,
                "openBugId": self.store.open_id, "session": self.store.session}

    async def _next(self, open_bug: dict | None) -> dict:
        """Spoken "next issue": settle the open ticket quickly and open a new one in the same session."""
        gesture = {"type": "gesture", "name": "next", "action": "ticket.next", "accepted": True, "closing": False,
                   "bugId": open_bug["id"] if open_bug else None}
        if open_bug is None:
            bug = await self._open_ticket("next")
            await self.send(["dashboard"], {**gesture, "bugId": bug["id"]})
            await self.upsert(bug)
            return {"ok": True, "action": "ticket.next", "accepted": True, "closing": False, "bug": bug,
                    "openBugId": bug["id"], "session": self.store.session}
        self.open_after_close = True
        if self.closing:  # already settling (a submit just before): the new ticket opens when it lands
            await self.send(["dashboard"], gesture)
            return {"ok": True, "action": "ticket.next", "accepted": True, "closing": True, "bug": open_bug,
                    "openBugId": self.store.open_id, "session": self.store.session}
        open_bug["closedBy"] = "next"
        await self.send(["app", "band"], {"type": "bug.closing", "bugId": open_bug["id"]})
        await self.send(["dashboard"], {**gesture, "closing": True})
        await self._begin_close(open_bug, grace=self.next_grace, strip_command=True)
        return {"ok": True, "action": "ticket.next", "accepted": True, "closing": True, "bug": open_bug,
                "openBugId": self.store.open_id, "session": self.store.session}

    async def on_complete(self, msg: dict) -> dict:
        """ticket.complete from the band's LLM: the description is finished, submit that ticket."""
        bug = self.store.open_bug
        if bug is None or (msg.get("bugId") and msg["bugId"] != bug["id"]):
            return {"ok": False, "error": "that ticket is not open"}
        if self.closing:
            return {"ok": True, "accepted": False, "closing": False, "bug": bug}
        bug["closedBy"] = "complete"
        if _str(msg.get("reason")).strip():
            bug["completeReason"] = msg["reason"].strip()
        await self.send(["app", "band"], {"type": "bug.closing", "bugId": bug["id"]})
        await self._begin_close(bug)
        return {"ok": True, "accepted": True, "closing": True, "bug": bug}

    async def _idle_watch(self, bug_id: str) -> None:
        """Safety nets: content and nothing new for ticket_idle -> submit; empty and idle
        for ticket_empty_idle -> cancel. Speech (interim too) and pointing reset the clock."""
        while True:
            bug = self.store.open_bug
            if bug is None or bug["id"] != bug_id or self.closing:
                return
            if self._speaking(time.monotonic()):
                self._idle_at = time.monotonic()  # still talking: not idle
            limit = self.ticket_idle if has_context(bug) else self.ticket_empty_idle
            wait = self._idle_at + limit - time.monotonic()
            if wait <= 0:
                break
            await asyncio.sleep(min(wait, 0.5))
        bug["closedBy"] = "idle"
        if has_context(bug):
            await self.send(["app", "band"], {"type": "bug.closing", "bugId": bug["id"]})
            await self._begin_close(bug)
        else:
            self.last_closed_id = bug["id"]
            await self._announce_closed(self.store.close("submitted"))  # settles as cancelled, reason "empty"

    def resume(self) -> None:
        """After a restart: keep watching a ticket that was open."""
        if self.store.open_id:
            self._idle_at = time.monotonic()
            self._spawn(self._idle_watch(self.store.open_id))

    def _speaking(self, now: float) -> bool:
        """The band says the person is talking (and the signal is not stuck)."""
        return self.speech_seen and self.speech_active and now - max(self.speech_at, self.tx_at) < SPEECH_STUCK_S

    def _should_settle(self, now: float) -> bool:
        """Has the person finished talking, so a closing ticket can be decided?"""
        c = self.closing
        if now - c["start"] >= c["cap"]:
            return True
        if self.speech_seen:  # the band reports speech activity
            if self._speaking(now):
                return False
            if c["flushed"]:  # the band forced the recogniser to finalise and everything has arrived
                return True
            return now - max(c["start"], self.speech_at, self.tx_at) >= c["settle"]
        # no speech signal (older band): go by the transcripts themselves
        if self.interim_pending and now - self.tx_at < self.quiet_window:
            return False  # mid-sentence
        if c["final"] and not self.interim_pending:
            return now - self.tx_at >= c["settle"]  # the final that followed the last interim
        return now - c["start"] >= c["grace"] and now - self.tx_at >= c["quiet"]

    async def _begin_close(self, bug: dict, grace: float | None = None, strip_command: bool = False) -> None:
        """A ticket is closing: the phone was told at once; now wait until the person has
        actually finished speaking (the recogniser often delivers the rest of a sentence
        seconds later) before deciding submitted vs. nothing captured -> cancelled."""
        bug["closing"] = True
        self.store.save()
        nxt = grace is not None  # "next": the sentence is known to be over, shorter limits
        self.closing = {"id": bug["id"], "strip": strip_command, "start": time.monotonic(), "final": False,
                        "flushed": False, "grace": self.next_grace if nxt else self.submit_grace,
                        "cap": self.next_cap if nxt else self.close_cap,
                        "settle": min(self.speech_settle, self.next_grace) if nxt else self.speech_settle,
                        "quiet": 0.0 if nxt else self.quiet_window}
        mine = self.closing
        await self.upsert(bug)

        async def later() -> None:
            while self.closing is mine and not self._should_settle(time.monotonic()):
                await asyncio.sleep(0.02)
            await self._finish_close(bug["id"])

        mine["task"] = self._spawn(later())

    async def _finish_close(self, bug_id: str) -> None:
        if not self.closing or self.closing["id"] != bug_id:
            return  # already settled, cancelled or reset
        strip = self.closing.get("strip") or self.open_after_close
        self.closing = None
        self.last_closed_id = bug_id
        bug = self.store.open_bug
        if bug is None or bug["id"] != bug_id:
            return
        if strip and bug["transcript"]:  # the spoken command is not part of the report
            last = bug["transcript"][-1]
            last["text"] = NEXT_PHRASE.sub("", _str(last.get("text"))).strip()
            if not last["text"]:
                bug["transcript"].pop()
        await self._announce_closed(self.store.close("submitted"))

    async def _announce_closed(self, bug: dict | None) -> None:
        if bug is None:
            return
        if bug["status"] == "submitted":
            await self.send(["app"], {"type": "bug.submitted", "bugId": bug["id"]})
            await self.send(["band", "agent"], {"type": "bug.submitted", "bugId": bug["id"], "bug": bug})
        else:
            await self.send(["app", "band", "agent"], {"type": "bug.cancelled", "bugId": bug["id"],
                                                       "reason": bug.get("cancelReason", "gesture")})
        await self.upsert(bug)
        if self.session_ending and self.store.open_id is None:
            self.open_after_close = False
            await self._end_session()
        elif self.open_after_close and self.store.open_id is None:
            self.open_after_close = False
            await self.upsert(await self._open_ticket("next"))

    async def wait_closed(self) -> None:
        task = (self.closing or {}).get("task")
        if task is not None:
            await asyncio.wait([task])

    async def on_speech(self, msg: dict) -> dict:
        """speech {active, flushed?} from the band: is the person talking right now?
        `flushed: true` (with active false) = the recogniser was forced to finalise and
        every final for what was said has been sent."""
        self.speech_seen = True
        active = msg.get("active") is True
        if active != self.speech_active:
            self.speech_at = time.monotonic()
        self.speech_active = active
        if active:
            self._idle_at = time.monotonic()
        elif msg.get("flushed") is True and self.closing:
            self.closing["flushed"] = True
        await self.send(["dashboard"], {"type": "speech", "active": active, "flushed": msg.get("flushed") is True})
        return {"ok": True, "active": active}

    def _late_target(self, wanted: str) -> dict | None:
        """The ticket a final belongs to when it arrives just after that ticket closed."""
        bug = self.store.bugs.get(wanted or self.last_closed_id or "")
        if bug is None or bug["status"] == "open" or not self.late_final:
            return None
        if now_ms() - int(bug.get("closedAt") or 0) > self.late_final * 1000:
            return None
        if bug["status"] == "cancelled" and bug.get("cancelReason") != "empty":
            return None  # the person cancelled it on purpose
        if bug.get("fix", {}).get("state") in ("done", "failed"):
            return None  # the agent already finished with the text it had
        return bug

    async def on_transcript(self, msg: dict) -> dict:
        text = _str(msg.get("text")).strip()
        if not text:
            return {"ok": False, "error": "text must be a non-empty string"}
        final = msg.get("final") is True
        wanted = _str(msg.get("bugId"))  # optional: the band may say which ticket the words belong to
        bug = self.store.open_bug
        self.tx_at = self._idle_at = time.monotonic()  # speech, even mid-sentence, keeps an open ticket alive
        self.interim_pending = not final
        late = None
        if final and (bug is None or (wanted and wanted != bug["id"])):
            # A band that reports speech also marks late words with their ticket; trust only the mark
            # then, so a question asked right after submitting is not glued onto the ticket.
            late = self._late_target(wanted) if (wanted or not self.speech_seen) else None
            if late is not None and not self._worth_amending(late, text):
                late = None
            if late is not None or wanted:
                bug = None  # not for the open ticket
        elif wanted and bug is not None and wanted != bug["id"]:
            bug = None
        out = {"type": "transcript", "text": text, "final": final,
               "bugId": late["id"] if late else bug["id"] if bug else None}
        if final:
            self.last_final = {"text": text, "at": now_ms()}
            if bug is not None and bug.get("openedBy") == "next" and not bug["transcript"] \
                    and now_ms() - bug["openedAt"] < NEXT_ECHO_MS and not NEXT_PHRASE.sub("", text).strip():
                bug = None  # just the "next issue" command landing late: shown as a caption, not stored
            if late is not None and not NEXT_PHRASE.sub("", text).strip():
                late = None  # same for a command echo after the ticket closed
            if bug is not None:
                bug["transcript"].append({"text": text, "at": now_ms()})
                self.store.save()
                if self.closing and self.closing["id"] == bug["id"]:
                    self.closing["final"] = True
        await self.send(["app", "dashboard"], out)
        if final and bug is not None:
            await self.upsert(bug)
        if late is not None:
            await self._amend(late, text)
        return {"ok": True, "appended": bool(final and (bug or late)), "amended": late is not None,
                "bugId": out["bugId"]}

    @staticmethod
    def _worth_amending(bug: dict, text: str) -> bool:
        """Late words must add something: not bare filler ("okay", "thanks"), and a ticket that
        was closed as empty is only revived by a real phrase of three words or more."""
        words = re.findall(r"[a-z']+", text.lower())
        if not words or all(w in FILLER for w in words):
            return False
        return bug["status"] != "cancelled" or len(words) >= 3

    async def _amend(self, bug: dict, text: str) -> None:
        """Words that arrived just after the ticket closed: add them and tell the band and agent again."""
        bug["transcript"].append({"text": text, "at": now_ms(), "amended": True})
        bug["amended"] = True
        revived = bug["status"] == "cancelled"  # it had been closed as empty; now it has content
        if revived:
            bug["status"] = "submitted"
            bug["submittedAt"] = now_ms()
            bug.pop("cancelReason", None)
        self.store.save()
        if revived:
            await self.send(["app"], {"type": "bug.submitted", "bugId": bug["id"], "amended": True})
        await self.send(["band", "agent"], {"type": "bug.submitted", "bugId": bug["id"], "bug": bug, "amended": True})
        await self.upsert(bug)

    async def _assist(self, msg: dict, element_id: str | None) -> dict:
        say = _str(msg.get("say")).strip()
        question = _str(msg.get("question")).strip()
        if not question and self.last_final and now_ms() - self.last_final["at"] < QUESTION_WINDOW_MS:
            question = self.last_final["text"]
        self.last_final = None  # one spoken line answers at most one assist
        entry = {"id": f"as_{secrets.token_hex(4)}", "at": now_ms(), "question": question,
                 "say": say, "elementId": element_id, "shown": None}
        self.assists.insert(0, entry)
        del self.assists[MAX_ASSISTS:]
        out = {"type": "assist" if element_id else "assist.none", "say": say,
               "question": question, "assistId": entry["id"]}
        if element_id:
            out["elementId"] = element_id
        await self.send(["app", "dashboard"], out)
        return {"ok": True, "assist": entry}

    async def on_assist(self, msg: dict) -> dict:
        element_id = _str(msg.get("elementId")).strip()
        if not element_id:
            return {"ok": False, "error": "elementId must be a non-empty string"}
        return await self._assist(msg, element_id)

    async def on_assist_none(self, msg: dict) -> dict:
        return await self._assist(msg, None)

    async def on_enrich(self, msg: dict) -> dict:
        bug = self.store.bugs.get(_str(msg.get("bugId")))
        if bug is None:
            return {"ok": False, "error": "unknown bugId"}
        for key in ("title", "summary"):
            if isinstance(msg.get(key), str):
                bug[key] = msg[key].strip()
        if msg.get("severity") in SEVERITIES:
            bug["severity"] = msg["severity"]
        if msg.get("kind") in KINDS:
            bug["kind"] = msg["kind"]
        if _strs(msg.get("elementIds")):
            bug["elementIds"] = list(dict.fromkeys(_strs(msg["elementIds"])))
            bug["enriched"] = True
        if isinstance(msg.get("steps"), list):
            bug["steps"] = _strs(msg["steps"])
        self.store.save()
        await self.upsert(bug, agent=True)
        return {"ok": True, "bug": bug}

    async def on_fix(self, msg: dict) -> dict:
        bug = self.store.bugs.get(_str(msg.get("bugId")))
        if bug is None:
            return {"ok": False, "error": "unknown bugId"}
        if msg.get("state") not in FIX_STATES:
            return {"ok": False, "error": f"state must be one of {', '.join(FIX_STATES)}"}
        fix = bug["fix"]
        if msg["state"] in ("queued", "working") and fix.get("state") not in ("queued", "working"):
            fix["startedAt"] = now_ms()  # lets the dashboard run a live timer
            fix["ms"] = 0
        fix["state"] = msg["state"]
        if isinstance(msg.get("summary"), str):
            fix["summary"] = msg["summary"].strip()
        if isinstance(msg.get("files"), list):
            fix["files"] = _strs(msg["files"])
        if isinstance(msg.get("ms"), (int, float)) and not isinstance(msg.get("ms"), bool):
            fix["ms"] = msg["ms"]
        elif msg["state"] in ("done", "failed") and fix.get("startedAt") and not fix.get("ms"):
            fix["ms"] = now_ms() - fix["startedAt"]  # the agent did not time itself
        self.store.save()
        await self.send(["app"], {"type": "bug.fix", "bugId": bug["id"], **fix})
        await self.upsert(bug)
        return {"ok": True, "bug": bug}

    async def on_app_context(self, msg: dict) -> None:
        ids = msg.get("visibleElementIds")
        tapped = msg.get("lastTappedElementId")
        self.app_context = {
            "screen": _str(msg.get("screen")),
            "visibleElementIds": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else [],
            "lastTappedElementId": tapped if isinstance(tapped, str) and tapped else None,
            "viewport": msg.get("viewport") if isinstance(msg.get("viewport"), dict) else None,
        }
        await self.send(["dashboard", "band"], {"type": "app.context", **self.app_context})

    async def on_interaction(self, msg: dict) -> dict:
        # bugId is optional on the debug route: it defaults to the open ticket.
        bug = self.store.bugs.get(_str(msg.get("bugId"))) if msg.get("bugId") else self.store.open_bug
        if bug is None:
            return {"ok": False, "error": "unknown bugId (and no ticket is open)"}
        if not isinstance(msg.get("items"), list):
            return {"ok": False, "error": "items must be a list"}
        # accepted for any known ticket: the last flush arrives just after submit
        added = self.store.add_interactions(bug, msg["items"])
        if added and bug["id"] == self.store.open_id:
            self._idle_at = time.monotonic()
        if added:
            await self.upsert(bug, agent=bug["status"] != "open")
        return {"ok": True, "added": added, "bug": bug}

    async def on_assist_shown(self, msg: dict) -> None:
        element_id = _str(msg.get("elementId"))
        ok = msg.get("ok") is True
        assist_id = None
        for entry in self.assists:
            if entry["elementId"] == element_id:
                entry["shown"] = ok
                assist_id = entry["id"]
                break
        await self.send(["dashboard"], {"type": "app.assist.shown", "elementId": element_id,
                                        "ok": ok, "assistId": assist_id})

    async def reset(self) -> dict:
        self.closing = None
        self.session_ending = False
        self.open_after_close = False
        self.store.reset()
        self.assists.clear()
        self.last_final = None
        for role in ROLES:
            await self.send([role], {"type": "hello", "role": role, "state": self.state()})
        return {"ok": True}

    ROUTES = {
        ("band", "gesture"): on_gesture,
        ("band", "ticket.complete"): on_complete,
        ("band", "transcript"): on_transcript,
        ("band", "speech"): on_speech,
        ("band", "assist"): on_assist,
        ("band", "assist.none"): on_assist_none,
        ("band", "bug.enrich"): on_enrich,
        ("app", "app.context"): on_app_context,
        ("app", "app.interaction"): on_interaction,
        ("agent", "bug.fix"): on_fix,
        ("app", "app.assist.shown"): on_assist_shown,
    }


# --------------------------------------------------------------------------- http


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _error(status: int, message: str) -> web.Response:
    return _json({"ok": False, "error": message}, status)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "body must be JSON"}),
                                 content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "body must be a JSON object"}),
                                 content_type="application/json")
    return data


async def ws_handler(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app[HUB]
    role = request.query.get("role")
    if role not in ROLES:
        return _error(400, f"role must be one of {', '.join(ROLES)}")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BODY)
    await ws.prepare(request)
    client_id = f"{role}_{secrets.token_hex(3)}@{request.remote or '?'}"
    try:
        await hub.connect(role, ws)  # inside the try: a client that vanishes during hello is still removed
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(frame.data)
            except ValueError:
                continue  # malformed JSON is ignored, the socket stays up
            try:
                if role == "app" and isinstance(msg, dict) and msg.get("type") == "app.diag":
                    if isinstance(msg.get("items"), list):  # phone diagnostics: logged, never fanned out
                        hub.store.append_diag(client_id, msg["items"])
                    continue
                await hub.handle(role, msg, ws)
            except Exception:
                log.exception("error handling %s message", role)
    finally:
        hub.disconnect(role, ws)
    return ws


async def api_state(request: web.Request) -> web.Response:
    return _json(request.app[HUB].state())


async def api_bugs(request: web.Request) -> web.Response:
    return _json(request.app[HUB].store.list())


async def api_bug(request: web.Request) -> web.Response:
    bug = request.app[HUB].store.bugs.get(request.match_info["id"])
    return _json(bug) if bug else _error(404, "no such bug")


async def api_kb(request: web.Request) -> web.Response:
    path: Path = request.app[KB_PATH]
    if not path.is_file():
        if "optional" in request.query:  # the dashboard polls this; an empty list keeps its console clean
            return _json([])
        return _error(404, f"knowledge base not found: {path} does not exist yet (the mobile app creates it)")
    try:
        return _json(json.loads(path.read_text("utf-8")))
    except ValueError as exc:
        return _error(500, f"knowledge base is not valid JSON: {exc}")


def _debug(method_name: str):
    async def handler(request: web.Request) -> web.Response:
        result = await getattr(request.app[HUB], method_name)(await _body(request))
        return _json(result, 200 if result.get("ok") else 400)
    return handler


async def api_debug_gesture(request: web.Request) -> web.Response:
    """Like the band's gesture, but the response waits for a submit to settle so it reports the outcome."""
    hub: Hub = request.app[HUB]
    result = await hub.on_gesture(await _body(request))
    if result.get("closing"):
        await hub.wait_closed()
        result["openBugId"] = hub.store.open_id
        result["session"] = hub.store.session
    return _json(result, 200 if result.get("ok") else 400)


async def api_band_control(request: web.Request) -> web.Response:
    result = await request.app[HUB].band_control(await _body(request))
    return _json(result, 200 if result.get("ok") else result.pop("status", 400))


async def api_debug_diag(request: web.Request) -> web.Response:
    try:
        n = max(1, min(5000, int(request.query.get("n", "200"))))
    except ValueError:
        n = 200
    return _json({"lines": request.app[HUB].store.read_diag(n)})


async def api_debug_complete(request: web.Request) -> web.Response:
    hub: Hub = request.app[HUB]
    result = await hub.on_complete(await _body(request))
    if result.get("closing"):
        await hub.wait_closed()
    return _json(result, 200 if result.get("ok") else 400)


async def _resume(app: web.Application) -> None:
    app[HUB].resume()


async def api_debug_assist(request: web.Request) -> web.Response:
    hub: Hub = request.app[HUB]
    body = await _body(request)
    if not _str(body.get("elementId")).strip() and _str(body.get("say")).strip():
        return _json(await hub.on_assist_none(body))  # no element: same as assist.none
    result = await hub.on_assist(body)
    return _json(result, 200 if result.get("ok") else 400)


async def api_debug_reset(request: web.Request) -> web.Response:
    return _json(await request.app[HUB].reset())


async def api_not_found(request: web.Request) -> web.Response:
    return _error(404, f"no such API route: {request.method} {request.path}")


APP_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Pineo</title>
<body style="font:16px/1.5 system-ui;background:#0b0d10;color:#e8ebf0;padding:40px;max-width:560px">
<h1>Pineo: app not built</h1>
<p>The hub is running, but <code>mobile-app/dist</code> does not exist yet.
Build it with <code>npm run build</code> in <code>mobile-app/</code>, then reload.</p>
<p>The dashboard is at <a style="color:#3ecf8e" href="/dashboard/">/dashboard/</a>.</p>"""

NO_CACHE = {"Cache-Control": "no-cache"}


def _static(root_key: web.AppKey, spa: bool, missing: str):
    async def handler(request: web.Request) -> web.StreamResponse:
        root: Path = request.app[root_key]
        index = root / "index.html"
        if not index.is_file():
            return web.Response(text=missing, content_type="text/html", status=503)
        rel = request.match_info.get("path", "")
        if rel:
            target = (root / rel).resolve()
            if target.is_file() and target.is_relative_to(root.resolve()):
                return web.FileResponse(target, headers=NO_CACHE)
            if not spa or "." in rel.rsplit("/", 1)[-1]:
                raise web.HTTPNotFound(text="not found")
        return web.FileResponse(index, headers=NO_CACHE)
    return handler


async def dashboard_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("/dashboard/")


@web.middleware
async def cors(request: web.Request, handler):
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


async def _close_sockets(app: web.Application) -> None:
    hub: Hub = app[HUB]
    for role in ROLES:
        for ws in list(hub.clients[role]):
            try:  # a client that is not reading must not hang the shutdown
                await asyncio.wait_for(ws.close(code=1001, message=b"hub shutting down"), 1)
            except Exception:
                pass


def create_app(data_dir: Path | None = None, app_dist: Path | None = None,
               dashboard_dir: Path | None = None, kb_path: Path | None = None,
               submit_grace: float | None = None, ticket_idle: float | None = None,
               ticket_empty_idle: float | None = None, timing: dict | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY, middlewares=[cors])
    if submit_grace is None:
        submit_grace = float(os.environ.get("PINEO_SUBMIT_GRACE", SUBMIT_GRACE_S))
    if ticket_idle is None:
        ticket_idle = float(os.environ.get("PINEO_TICKET_IDLE", TICKET_IDLE_S))
    if ticket_empty_idle is None:
        ticket_empty_idle = float(os.environ.get("PINEO_TICKET_EMPTY_IDLE", TICKET_EMPTY_IDLE_S))
    app[HUB] = Hub(Store(data_dir or ROOT / "hub" / "data"), submit_grace=submit_grace,
                   ticket_idle=ticket_idle, ticket_empty_idle=ticket_empty_idle)
    for name, env in (("close_cap", "PINEO_CLOSE_CAP"), ("speech_settle", "PINEO_SPEECH_SETTLE"),
                      ("quiet_window", "PINEO_QUIET_WINDOW"), ("late_final", "PINEO_LATE_FINAL"),
                      ("next_grace", "PINEO_NEXT_GRACE"), ("next_cap", "PINEO_NEXT_CAP")):
        if timing and name in timing:
            setattr(app[HUB], name, float(timing[name]))
        elif os.environ.get(env):
            setattr(app[HUB], name, float(os.environ[env]))
    app.on_startup.append(_resume)
    app[APP_DIST] = Path(app_dist or ROOT / "mobile-app" / "dist")
    app[DASHBOARD_DIR] = Path(dashboard_dir or ROOT / "dashboard")
    app[KB_PATH] = Path(kb_path or ROOT / "mobile-app" / "src" / "pineo" / "elements.json")
    app.on_shutdown.append(_close_sockets)
    r = app.router
    r.add_get("/ws", ws_handler)
    r.add_get("/api/state", api_state)
    r.add_get("/api/bugs", api_bugs)
    r.add_get("/api/bugs/{id}", api_bug)
    r.add_get("/api/kb", api_kb)
    r.add_post("/api/band/control", api_band_control)
    r.add_post("/api/debug/gesture", api_debug_gesture)
    r.add_post("/api/debug/transcript", _debug("on_transcript"))
    r.add_post("/api/debug/speech", _debug("on_speech"))
    r.add_post("/api/debug/assist", api_debug_assist)
    r.add_post("/api/debug/complete", api_debug_complete)
    r.add_get("/api/debug/diag", api_debug_diag)
    r.add_post("/api/debug/enrich", _debug("on_enrich"))
    r.add_post("/api/debug/interaction", _debug("on_interaction"))
    r.add_post("/api/debug/fix", _debug("on_fix"))
    r.add_post("/api/debug/band", _debug("on_band_state"))
    r.add_post("/api/debug/reset", api_debug_reset)
    r.add_route("*", "/api/{tail:.*}", api_not_found)
    r.add_get("/dashboard", dashboard_redirect)
    r.add_get("/dashboard/{path:.*}", _static(DASHBOARD_DIR, False, "dashboard folder is missing"))
    r.add_get("/{path:.*}", _static(APP_DIST, True, APP_NOT_BUILT))
    return app
