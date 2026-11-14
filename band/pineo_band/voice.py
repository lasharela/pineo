"""The voice pipeline, on Pipecat (pipecat-ai 1.16):

    BandMicInput -> DeepgramSTTService (nova-3) -> UtteranceRouter -> OpenAILLMService (MiniMax,
    function calling) -> AnswerSink

- BandMicInput     the pipeline's custom input. The band's Opus packets are decoded to 16 kHz
                   PCM by the kit's PcmDecoder and queued here as InputAudioRawFrame (the
                   laptop microphone and typed utterances enter the same way).
- UtteranceRouter  forwards every interim and final transcript to the hub; joins Deepgram's
                   final pieces into one utterance; while no bug is open, turns the utterance
                   into an LLMContextFrame (compact knowledge base + what is on the phone).
                   It also feeds the one write-up request after a bug is submitted.
- OpenAILLMService MiniMax through its OpenAI-compatible API. The tools are real function
                   calls; their handler sends `assist` / `assist.none` / `bug.enrich` to the hub.
- AnswerSink       reads what the model said when it called no tool: "SKIP" (silence), or
                   the strict-JSON fallback, and frees the router for the next request.

A second, small pipeline (UtteranceRouter -> OpenAILLMService -> AnswerSink) on the same
runner writes up submitted tickets, so questions and write-ups never wait for each other.
While a ticket is open, only utterances that look like navigation questions go to the model
(task "assist-strict": an element or silence), and "close this ticket" / "never mind" said
alone become `submit` / `cancel` gestures.

Audio never waits for the model: Pipecat runs every processor on its own task, and the
router keeps at most one question in flight plus the newest one waiting.
"""
import asyncio
import time
from collections import deque

from loguru import logger
from pipecat.frames.frames import (EndFrame, ErrorFrame, Frame, FunctionCallsStartedFrame, InputAudioRawFrame,
                                   InterimTranscriptionFrame, LLMContextFrame, LLMFullResponseEndFrame,
                                   LLMFullResponseStartFrame, LLMTextFrame, TranscriptionFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from . import brain

SAMPLE_RATE = 16000
INTERIM_GAP = 0.15            # at most ~7 interim transcripts a second to the hub
JOIN_SECONDS = 0.7            # Deepgram cuts a sentence into pieces at short pauses: pieces closer than this are one utterance
QUIET_SECONDS = 1.2           # no interim or final text for this long: the person has stopped speaking
FLUSH_SECONDS = 1.0           # how long to wait for Deepgram's answer to Finalize
AFTER_CLOSE_SECONDS = 4.0     # a continuation that arrives this soon after a ticket closed still belongs to it
HOLD_SECONDS = 3.5            # a question asked during a ticket waits this long for its answer before it joins the ticket
GRACE_SECONDS = 1.5           # quiet time after "complete" before the ticket is closed
LLM_CLIENT_TIMEOUT = 15.0     # the HTTP client gives up on one model call after this (no retries)
LLM_TIMEOUT = 25.0
LATE_SECONDS = 10.0           # an answer older than this is not shown: the person has moved on
STALE_SECONDS = 6.0           # a question that waited this long behind another is dropped: the moment has passed


class BandMicInput(FrameProcessor):
    """Head of the pipeline. Audio frames queued on the worker arrive here; this counts them
    and lets them through only while listening."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.listening = True
        self.frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if not self.listening:
                return
            self.frames += 1
        await self.push_frame(frame, direction)


class Request:
    def __init__(self, kind, text="", bug=None, strict=False) -> None:
        self.kind, self.text, self.bug = kind, text, bug          # "assist" | "ticket"
        self.strict = strict        # asked while a ticket was open: an element or silence
        self.held = []              # finals kept back from the ticket until we know whether this was answered
        self.bug_id = None          # "status": the ticket it is about
        self.seq = None             # "status": the speech counter when it was asked
        self.made = time.monotonic()
        self.heard = None           # when the first interim of this utterance arrived
        self.final = None           # when its last final piece arrived
        self.sent = None            # when it went to the model
        self.tool = None            # the tool the model called, if any
        self.done = False


class UtteranceRouter(FrameProcessor):
    def __init__(self, hub, log, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hub, self.log = hub, log
        self.current = None                 # the Request at the model
        self.dispatched = deque()           # requests in the order their contexts went to the model (one response each)
        self.waiting_assist = None          # newest question, while the model is busy
        self.waiting_tickets = []
        self.idle = asyncio.Event()
        self.idle.set()
        self._pieces, self._join_task = [], None
        self._held = []                     # pieces of the current utterance not yet sent as finals
        self._interim_at, self._heard, self._final = 0.0, None, None
        self._watchdog = None
        self.timings = []                   # dicts, one per finished request: for the log and the tests
        self.on_command = None              # fn("submit" | "cancel") after a spoken command was sent
        self.speech_active = False          # told to the hub as `speech {active}`
        self._quiet_task = None
        self._flush_event = None            # set when a final marked "from Finalize" arrived
        self.closed_id, self.closed_at = None, 0.0     # the ticket that closed last, for late continuations
        self.audio_seconds = lambda: None   # how much audio has been sent to the speech service (for the lag log)
        self.lags = []                      # ms between the end of a piece in the audio and its final arriving
        self.on_speech = None               # fn() on every interim or final piece: the person is talking
        self.on_feedback = None             # fn(text) for an utterance that belongs to the open ticket
        self.on_status = None               # fn(request, args) for the completeness lane

    # ---- transcripts

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and frame.text.strip():
            self._speaking(getattr(frame, "result", None) is not None)
            if self.on_speech:
                self.on_speech()
        if isinstance(frame, InterimTranscriptionFrame):
            now = time.monotonic()
            if self._heard is None:
                self._heard = now
            if frame.text.strip() and now - self._interim_at >= INTERIM_GAP:
                self._interim_at = now
                self.hub.send("transcript", text=frame.text.strip(), final=False)
            if self._join_task and self._pieces:
                self._restart_join()        # still talking: wait for the next final piece
            return
        if isinstance(frame, TranscriptionFrame):
            await self._on_final(frame)
            return
        if isinstance(frame, ErrorFrame) and direction == FrameDirection.UPSTREAM:
            self.log("voice: %s" % str(frame.error)[:200])
            if self.current is not None and "LLM" in str(getattr(frame, "processor", "")):
                await self.finished(self.current, failed=True)
        if isinstance(frame, EndFrame) and self._pieces:
            await self._flush()
        await self.push_frame(frame, direction)

    # ---- is the person speaking? (the hub holds a closing ticket open until they are not)

    def _speaking(self, live: bool) -> None:
        if not live:                         # typed utterances are whole at once: nothing is "in progress"
            return
        if not self.speech_active:
            self.speech_active = True
            self.hub.send("speech", active=True)
        if self._quiet_task:
            self._quiet_task.cancel()
        self._quiet_task = asyncio.ensure_future(self._quiet_later())

    async def _quiet_later(self) -> None:
        try:
            await asyncio.sleep(QUIET_SECONDS)
        except asyncio.CancelledError:
            return
        self._quiet_task = None
        self._speech_over()

    def _speech_over(self, flushed: bool = False) -> None:
        if self._quiet_task:
            self._quiet_task.cancel()
            self._quiet_task = None
        if self.speech_active or flushed:
            self.speech_active = False
            self.hub.send("speech", active=False, flushed=True if flushed else None)

    async def flushed(self, asked: bool) -> None:
        """After Finalize was sent to the speech service (asked=True): wait for the final it forces
        out, put the pieces together, and tell the hub that nothing more is coming."""
        if asked:
            self._flush_event = asyncio.Event()
            try:
                await asyncio.wait_for(self._flush_event.wait(), FLUSH_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._flush_event = None
            await asyncio.sleep(0.05)        # the final itself is handled just after the mark
        if self._pieces:
            await self._flush()
        self._speech_over(flushed=True)

    def voice_command(self, command: str, source: str, said: str, took_ms: int, first_words=None) -> bool:
        """A command understood from speech ("phrase" = exact wording, "model" = MiniMax) -> a gesture.
        Checked against the state again here, because a model's answer arrives later."""
        state = self.hub.state
        allowed = {"open": bool(state.session) and not state.bug_open, "end": bool(state.session), "next": bool(state.session),
                   "submit": state.gesture_open, "cancel": state.gesture_open}.get(command, False)
        if not allowed:
            self.log('voice command %s (%s) not possible now: "%s"' % (command.upper(), source, said))
            return False
        self.hub.send("gesture", name={"end": "double"}.get(command, command))
        self.log('VOICE COMMAND %s (%s, %d ms): "%s"' % (command.upper(), source, took_ms, said))
        self.timings.append({"kind": "command", "tool": command, "source": source, "llm_ms": took_ms})
        if command == "open" and first_words:
            asyncio.ensure_future(self._first_words(first_words))
        if self.on_command:
            self.on_command(command)
        return True

    async def _first_words(self, text: str) -> None:
        """The report that came in the same breath as "open a ticket": the ticket's first words, once it is open."""
        for _ in range(40):
            if self.hub.state.bug_open:
                break
            await asyncio.sleep(0.05)
        if not self.hub.state.bug_open:
            self.log('the hub opened no ticket, so these words were not filed: "%s"' % text)
            return
        self.hub.send("transcript", text=text, final=True)
        self.log('first words of the new ticket: "%s"' % text)
        if self.on_feedback:
            self.on_feedback(text)

    def ticket_closed(self, bug_id) -> None:
        self.closed_id, self.closed_at = bug_id, time.monotonic()

    async def _on_final(self, frame: TranscriptionFrame) -> None:
        text = " ".join(frame.text.split())
        if getattr(frame, "finalized", False) and self._flush_event is not None:
            self._flush_event.set()
        if not text:
            return
        now = time.monotonic()
        result = getattr(frame, "result", None)
        sent, start, length = self.audio_seconds(), getattr(result, "start", None), getattr(result, "duration", None)
        if sent is not None and isinstance(start, (int, float)) and isinstance(length, (int, float)):
            lag = round((sent - (start + length)) * 1000)
            self.lags.append(lag)
            self.log('final after %d ms: "%s"' % (lag, text))
        if (not self.hub.state.bug_open and self.closed_id and now - self.closed_at < AFTER_CLOSE_SECONDS
                and not self._pieces and brain.is_late_part(text)):
            # the rest of a sentence whose ticket has just closed: it belongs there, and is not a question
            self.hub.send("transcript", text=text, final=True, bugId=self.closed_id)
            self.log('late words for the ticket that just closed (%s): "%s"' % (self.closed_id, text))
            return
        state = self.hub.state
        command, spoken_as, first_words = brain.spoken_command(text), text, None
        alone = not self._pieces
        if command == "next":                                   # valid in a session, ticket open or not
            command = "next" if state.session else None
        elif command == "open":                                 # by voice, no gesture: only in a session with no ticket
            command = "open" if state.session and not state.bug_open and alone else None
        elif command == "end":
            command = "end" if state.session and alone else None
        elif command and (not alone or not state.gesture_open):
            command = None                                      # submit / cancel: only alone, only on an open ticket
        if command is None and state.session:
            tail = brain.trailing_next(text)                    # "...should be green. Next issue."
            if tail:
                text, spoken_as = tail
                command = "next"
                self.hub.send("transcript", text=text, final=True)      # this part belongs to the current ticket
                self._pieces.append(text)
        if command is None and state.session and not state.bug_open and alone:
            first_words = brain.leading_open(text)              # "Open a ticket: the submit button is too small"
            if first_words:
                command = "open"
        if command:                                             # a command, not part of any ticket
            self.hub.send("transcript", text=spoken_as, final=False)
            if self._pieces:
                self._final = now
                await self._flush()
            self.voice_command(command, "phrase", spoken_as, 0, first_words)
            self._heard = self._final = None
            return
        if state.bug_open and ((state.kb and brain.looks_like_navigation(text)) or brain.maybe_command(text)):
            # Maybe a question to the assistant, or an instruction about the ticket ("go ahead and file
            # it"): if it is, it must stay out of the ticket, so it is shown now (non-final) and only
            # becomes part of the ticket if the model says it is neither.
            self.hub.send("transcript", text=text, final=False)
            self._held.append(text)
        else:
            self.hub.send("transcript", text=text, final=True)     # the hub appends finals to an open bug
        self._pieces.append(text)
        self._final = now
        if self._heard is None:
            self._heard = now
        if result is None:                                      # typed utterances have no result: whole at once
            await self._flush()
        else:                                                   # a pause is not the end: wait a moment for the next piece
            self._restart_join()

    def _restart_join(self) -> None:
        if self._join_task:
            self._join_task.cancel()
        self._join_task = asyncio.ensure_future(self._join_later())

    async def _join_later(self) -> None:
        try:
            await asyncio.sleep(JOIN_SECONDS)
        except asyncio.CancelledError:
            return
        self._join_task = None
        await self._flush()

    async def _flush(self) -> None:
        if self._join_task:
            self._join_task.cancel()
            self._join_task = None
        text, self._pieces = " ".join(self._pieces), []
        held, self._held = self._held, []
        heard, final, self._heard, self._final = self._heard, self._final, None, None
        if not text:
            return
        request = self._maybe_ask(text, heard, final)
        if request is None:
            self._release(held)
            return
        request.held = held
        if held:
            asyncio.ensure_future(self._release_later(request))
        if self.current is None:
            await self._dispatch(request)
        else:
            if self.waiting_assist is not None:
                self.log('dropped (a newer one came while the model was busy): "%s"' % self.waiting_assist.text)
                self._release_request(self.waiting_assist)
            self.waiting_assist = request

    def _release(self, held) -> None:
        for text in held:                   # not answered: it is part of the ticket after all
            self.hub.send("transcript", text=text, final=True)

    def _release_request(self, request) -> None:
        held, request.held = request.held, []
        self._release(held)

    async def _release_later(self, request) -> None:
        await asyncio.sleep(HOLD_SECONDS)
        if request.held:
            self.log('no answer within %g s, so it goes into the ticket: "%s"' % (HOLD_SECONDS, request.text))
            self._release_request(request)

    def _maybe_ask(self, text, heard, final):
        """The utterance as a Request for the model, or None when it is not asked."""
        if not self.hub.state.session and not self.hub.state.bug_open:
            self.log('heard, but no session is active (rotate twice to start one): "%s"' % text)
            return None
        strict = self.hub.state.bug_open
        if strict and brain.maybe_command(text) and not (self.hub.state.closing and brain.is_late_part(text)):
            request = Request("command", text)                  # short, and worded like an instruction: the model decides
            request.heard, request.final = heard, final
            return request
        if strict and (not brain.looks_like_navigation(text) or (self.hub.state.closing and brain.is_late_part(text))):
            self.log('heard (ticket open, feedback): "%s"' % text)
            if self.on_feedback:
                self.on_feedback(text)
            return None
        if not brain.worth_asking(text):
            self.log('heard (too short to be a question): "%s"' % text)
            return None
        if not self.hub.state.kb:
            self.log('heard, but there is no knowledge base yet: "%s"' % text)
            return None
        request = Request("assist", text, strict=strict)
        request.heard, request.final = heard, final
        return request

    # ---- requests to the model

    async def write_ticket(self, bug: dict) -> None:
        request = Request("ticket", bug=bug)
        if self.current is None:
            await self._dispatch(request)
        else:
            self.waiting_tickets.append(request)

    async def ask_status(self, request: Request) -> None:
        """The completeness lane: one in flight, and only the newest one waits."""
        if self.current is None:
            await self._dispatch(request)
        else:
            self.waiting_assist = request

    async def _dispatch(self, request: Request) -> None:
        state = self.hub.state
        if request.kind == "status":
            context = LLMContext(brain.status_messages(request.text, request.bug, state.kb), tools=brain.status_tools())
        elif request.kind == "command":
            context = LLMContext(brain.command_messages(request.text), tools=brain.command_tools())
        elif request.kind == "assist":
            context = LLMContext(brain.assist_messages(request.text, state.kb, state.app_context, request.strict),
                                 tools=brain.assist_tools())
        else:
            context = LLMContext(brain.ticket_messages(request.bug, state.kb), tools=brain.ticket_tools())
        context.pineo_request = request
        self.current = request
        self.idle.clear()
        request.sent = time.monotonic()
        self._watchdog = asyncio.ensure_future(self._give_up(request))
        self.dispatched.append(request)
        await self.push_frame(LLMContextFrame(context))

    async def _give_up(self, request: Request) -> None:
        try:
            await asyncio.sleep(LLM_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self.current is request:
            self.log("voice: the model did not answer in %g s." % LLM_TIMEOUT)
            await self.finished(request, failed=True)

    def act(self, request: Request, tool: str, args) -> None:
        """A tool call (or its JSON fallback) for `request` -> one message to the hub."""
        if request.tool is not None:
            return
        request.tool = tool
        now = time.monotonic()
        if request.kind == "status":
            if tool == "ticket_status" and self.on_status:
                self.on_status(request, args, round((now - request.sent) * 1000))
            return
        took = round((now - request.sent) * 1000)
        if request.kind == "command":
            command = brain.command_result(args) if tool == "ticket_command" else None
            if command and now - request.sent <= LATE_SECONDS and self.voice_command(command, "model", request.text, took):
                request.held = []                         # an instruction: it stays out of the ticket
            else:
                self.log('not an instruction (%d ms), so it is ticket text: "%s"' % (took, request.text))
                self._release_request(request)
                if self.on_feedback:
                    self.on_feedback(request.text)
            return
        if request.kind == "assist" and tool in ("open_ticket", "end_session"):
            if now - request.sent <= LATE_SECONDS:
                self.voice_command("open" if tool == "open_ticket" else "end", "model", request.text, took,
                                   brain.open_result(args) if tool == "open_ticket" else None)
            self._release_request(request)
            return
        if request.kind == "assist":
            if now - request.sent > LATE_SECONDS:
                self.log("the answer came too late (%.0f s): not sent." % (now - request.sent))
                self._release_request(request)
                return
            strict = request.strict or self.hub.state.bug_open      # also when a ticket was opened meanwhile
            found = brain.assist_result(tool, args, self.hub.state.kb_ids, strict)
            if found is None:
                self.log('no element to show while a ticket is open: "%s"' % request.text)
                self._release_request(request)
                return
            kind, fields = found
            if kind == "assist.none" and not brain.not_found_allowed(request.text):
                self.log('not addressed to the app, staying silent: "%s"' % request.text)
                self._release_request(request)
                return
            if kind == "assist" and request.held:
                self.log('answered, so it stays out of the ticket: "%s"' % request.text)
                request.held = []
            else:
                self._release_request(request)
            self.hub.send(kind, question=request.text, **fields)
            self.log('%s -> %s  "%s"' % (kind.upper(), fields.get("elementId", "-"), fields["say"]))
        elif tool == "write_ticket":
            fields = brain.ticket_result(args, request.bug, self.hub.state.kb_ids)
            self.hub.send("bug.enrich", **fields)
            self.log('TICKET %s: [%s/%s] "%s" elements=%s' % (fields["bugId"], fields["kind"], fields["severity"],
                                                             fields["title"], ",".join(fields["elementIds"]) or "-"))
        timing = {"kind": request.kind, "tool": tool, "llm_ms": round((now - request.sent) * 1000)}
        if request.final is not None:
            timing["stt_ms"] = round((request.final - request.heard) * 1000)       # first words heard -> final text
            timing["wait_ms"] = round((request.sent - request.final) * 1000)       # end-of-utterance join + queue
            timing["total_ms"] = round((now - request.final) * 1000)               # final text -> message to the hub
        self.timings.append(timing)
        self.log("timing: " + "  ".join("%s=%s" % item for item in timing.items()))

    async def finished(self, request: Request, failed=False, text="") -> None:
        """The model's turn for `request` is over (tool call, SKIP, or failure)."""
        if request.done:
            return
        if request.tool is None or failed:
            self._release_request(request)            # no answer was given: the words belong to the ticket
        request.done = True                           # a late tool call for it is still acted on, once (see act)
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if request.tool is None:
            fallback = None if failed else brain.parse_fallback(text)
            if fallback is not None:
                self.act(request, *fallback)
            elif request.kind == "status":
                pass                                  # no verdict: do nothing, the hub's idle timer is the safety net
            elif request.kind == "ticket":            # a ticket always gets a write-up, even without the model
                self.act(request, "write_ticket", {})
                self.log("the model wrote no ticket (%s): filled in from the transcript." % ("failed" if failed else "no tool call"))
            else:
                took = round((time.monotonic() - request.sent) * 1000)
                self.timings.append({"kind": "assist", "tool": None, "llm_ms": took})
                self.log('%s (%d ms): "%s"' % ("model failed, nothing sent" if failed else "not a question for the app", took, request.text))
        if self.current is request:
            self.current = None
        await self._advance()

    async def _advance(self) -> None:
        if self.current is not None:
            return
        if self.waiting_tickets:
            await self._dispatch(self.waiting_tickets.pop(0))
            return
        request, self.waiting_assist = self.waiting_assist, None
        if request is not None:
            if time.monotonic() - request.made > STALE_SECONDS and request.kind != "status":
                self.log('dropped (waited too long): "%s"' % request.text)
                self._release_request(request)
            else:
                await self._dispatch(request)
                return
        self.idle.set()


class TicketWatcher:
    """THE AI CLOSES THE TICKET. After each finished utterance that belongs to the open ticket, one
    small model call decides whether the report is complete. If it is, and the person stays quiet
    for GRACE_SECONDS, `ticket.complete` goes to the hub. Any new speech cancels it; when unsure,
    nothing happens (the hub's idle timers are the safety net)."""

    def __init__(self, hub, lane: "UtteranceRouter", log) -> None:
        self.hub, self.lane, self.log = hub, lane, log
        self.seq = 0                         # goes up with every piece of speech
        self.said = {}                       # bugId -> [utterances], for when the hub cannot be asked
        self._grace = None
        self.sent = set()                    # bugIds already declared complete
        self.last_speech = time.monotonic()
        lane.on_status = self.verdict

    def speech(self) -> None:
        self.seq += 1
        self.last_speech = time.monotonic()
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None
            self.log("ticket: still talking, not closing yet.")

    def feedback(self, text: str) -> None:
        state = self.hub.state
        bug_id = state.open_bug_id
        if bug_id is None or state.closing or bug_id in self.sent:
            return
        self.said.setdefault(bug_id, []).append(text)
        asyncio.ensure_future(self._check(bug_id, self.seq))

    async def _check(self, bug_id, seq) -> None:
        bug = await self.hub.fetch_bug(bug_id) if self.hub.connected.is_set() else None
        said = brain.spoken(bug) if bug else ""
        if not said:
            said = " ".join(self.said.get(bug_id, []))
        if seq != self.seq or self.hub.state.open_bug_id != bug_id:
            return
        if not brain.may_be_complete(said):
            self.log("ticket: not complete yet (short or trailing off), model not asked.")
            return
        request = Request("status", said, bug=bug or {})
        request.bug_id, request.seq = bug_id, seq
        await self.lane.ask_status(request)

    def verdict(self, request, args, took_ms) -> None:
        complete, reason = brain.status_result(args)
        state = self.hub.state
        stale = request.seq != self.seq or state.open_bug_id != request.bug_id or state.closing
        self.log("ticket status: %s (%s) in %d ms%s" % ("COMPLETE" if complete else "not complete", reason or "-", took_ms,
                                                        "  [ignored: more was said meanwhile]" if stale else ""))
        if not complete or stale or request.bug_id in self.sent:
            return
        if self._grace is not None:
            self._grace.cancel()
        self._grace = asyncio.ensure_future(self._later(request.bug_id, request.seq, reason))

    async def _later(self, bug_id, seq, reason) -> None:
        try:                                 # the quiet time counts from the last word, not from the verdict
            await asyncio.sleep(max(0.0, GRACE_SECONDS - (time.monotonic() - self.last_speech)))
        except asyncio.CancelledError:
            return
        self._grace = None
        state = self.hub.state
        if seq != self.seq or state.open_bug_id != bug_id or state.closing or bug_id in self.sent:
            return
        self.sent.add(bug_id)
        self.hub.send("ticket.complete", bugId=bug_id, reason=reason or "the report is complete")
        self.log("TICKET COMPLETE (%s): %s" % (bug_id, reason or "the report is complete"))


class AnswerSink(FrameProcessor):
    """After the model: collects its text, and tells the router when its turn is over."""

    def __init__(self, router: UtteranceRouter, **kwargs) -> None:
        super().__init__(**kwargs)
        self.router = router
        self._text, self._calls = [], False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._text, self._calls = [], False
        elif isinstance(frame, LLMTextFrame):
            self._text.append(frame.text)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._calls = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            # The response that just ended belongs to the oldest context sent, which after a timeout is
            # NOT necessarily the request the router is waiting for now.
            request = self.router.dispatched.popleft() if self.router.dispatched else None
            if request is not None:
                if self._calls and request.tool is None:
                    for _ in range(40):                    # the tool handler runs on its own task: let it land
                        if request.tool is not None:
                            break
                        await asyncio.sleep(0.025)
                await self.router.finished(request, text="".join(self._text))
        await self.push_frame(frame, direction)


class Voice:
    """Builds and runs the pipeline. `stt=False` leaves Deepgram out (typed utterances only:
    no audio leaves the computer and no Deepgram credit is used)."""

    def __init__(self, hub, minimax: dict, deepgram_key: str = "", stt: bool = True, endpointing: int = 300,
                 language: str = "en", log=print, llm=None, ticket_llm=None, status_llm=None, keyterms=None,
                 ai_close: bool = False) -> None:
        self.hub, self.log = hub, log
        self.keyterms = list(keyterms or [])
        self.mic = BandMicInput()
        self.router = UtteranceRouter(hub, log)
        self._flushed_at = 0.0
        self.sink = AnswerSink(self.router)
        self.llm = llm or self._minimax(minimax)
        self.llm.register_function(None, self._on_tool)
        # The write-up lane: its own small pipeline with its own MiniMax service, so a ticket being
        # written (several seconds) never delays a question, and a question never delays a ticket.
        self.ticket_router = UtteranceRouter(hub, log)
        self.ticket_llm = ticket_llm or self._minimax(minimax)
        self.ticket_llm.register_function(None, self._on_tool)
        self.ticket_worker = PipelineWorker(Pipeline([self.ticket_router, self.ticket_llm, AnswerSink(self.ticket_router)]),
                                            idle_timeout_secs=None, enable_rtvi=False, params=PipelineParams(enable_metrics=False))
        # The completeness lane (off unless PINEO_AI_CLOSE=1: the owner prefers "next issue", the single
        # rotation or the hub's idle timer). When off, nothing of it is built or called.
        self.ai_close = ai_close
        self.status_router = self.status_worker = self.watcher = None
        if ai_close:
            self.status_router = UtteranceRouter(hub, log)
            self.status_llm = status_llm or self._minimax(minimax)
            self.status_llm.register_function(None, self._on_tool)
            self.status_worker = PipelineWorker(Pipeline([self.status_router, self.status_llm, AnswerSink(self.status_router)]),
                                                idle_timeout_secs=None, enable_rtvi=False, params=PipelineParams(enable_metrics=False))
            self.watcher = TicketWatcher(hub, self.status_router, log)
            self.router.on_speech = self.watcher.speech
            self.router.on_feedback = self.watcher.feedback
        parts = [self.mic]
        self.stt = None
        if stt:
            self.stt = self._deepgram(deepgram_key, endpointing, language)
            parts.append(self.stt)
            self.router.audio_seconds = lambda: self.mic.frames * 0.02
        parts += [self.router, self.llm, self.sink]
        self.worker = PipelineWorker(Pipeline(parts), idle_timeout_secs=None, enable_rtvi=False,
                                     params=PipelineParams(audio_in_sample_rate=SAMPLE_RATE, enable_metrics=False))
        self.runner = WorkerRunner(handle_sigint=False)
        self._loop = None
        self.started = asyncio.Event()

    @staticmethod
    def _minimax(settings: dict):
        from pipecat.services.openai.llm import OpenAILLMService
        # A reasoning model: thinking spends tokens too, so the budget is generous; temperature is left alone.
        service = OpenAILLMService(api_key=settings["key"], base_url=settings["base_url"],
                                settings=OpenAILLMService.Settings(model=settings["model"], max_tokens=2000,
                                                                   system_instruction=brain.SYSTEM,
                                                                   extra=dict(settings.get("extra") or {})))
        try:    # the OpenAI client's defaults are a 600 s timeout and 2 retries: one hung call would block this lane for minutes
            service._client = service._client.with_options(timeout=LLM_CLIENT_TIMEOUT, max_retries=0)
        except Exception as problem:
            logger.warning("could not shorten the model client's timeout: %s" % problem)
        return service

    def _deepgram(self, key: str, endpointing: int, language: str):
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.utils.network import QuickFailureResult, QuickFailureTracker

        class NeverGiveUp(QuickFailureTracker):
            """Pipecat 1.16 stops reconnecting to Deepgram for good after 3 quick failures (about 10 s
            without network), leaving a pipeline that runs and hears nothing. On stage the Wi-Fi may
            drop for longer than that, so this one keeps counting (for the back-off) and never trips.
            A key Deepgram rejects (4xx) still ends it at once: that path does not use the tracker."""

            def record(self, duration: float) -> QuickFailureResult:
                result = super().record(duration)
                self.count = min(self.count, 4)
                return QuickFailureResult(is_quick_failure=result.is_quick_failure, should_give_up=False)

        class PatientDeepgram(DeepgramSTTService):
            async def _connect(self):
                # The stock version waits until connecting has "resolved", which with a tracker that
                # never gives up would hold the whole pipeline while the network is down.
                self._quick_failure_tracker.reset()
                self._connection_settled.clear()
                self._connection_task = self.create_task(self._connection_handler())
                try:
                    await asyncio.wait_for(self._connection_settled.wait(), 6)
                except asyncio.TimeoutError:
                    pass

        DeepgramSTTService = PatientDeepgram
        terms = self.keyterms                             # the app's own words, so Deepgram expects them
        fields = dict(model="nova-3", language=language, interim_results=True, smart_format=True, punctuate=True,
                      endpointing=endpointing)
        if terms:
            fields["keyterm"] = terms
        service = DeepgramSTTService(api_key=key, sample_rate=SAMPLE_RATE, settings=DeepgramSTTService.Settings(**fields))
        service._quick_failure_tracker = NeverGiveUp()
        return service

    @property
    def stt_state(self):
        """"ok" | "reconnecting" | None (no speech service in this run)."""
        if self.stt is None:
            return None
        return "ok" if getattr(self.stt, "_connection", None) is not None else "reconnecting"

    async def watch_stt(self, on_change, every: float = 0.5) -> None:
        """Says so, once, when Deepgram goes away and when it is back."""
        last, since = self.stt_state, time.monotonic()        # still connecting at start counts as "reconnecting"
        while True:
            await asyncio.sleep(every)
            now = self.stt_state
            if now is None or now == last:
                continue
            if now == "reconnecting":
                self.log("SPEECH SERVICE DOWN: Deepgram is not connected (network?). Reconnecting until it is back; "
                         "nothing is transcribed meanwhile. Gestures still work.")
            else:
                self.log("speech service back after %.0f s." % (time.monotonic() - since))
            last, since = now, time.monotonic()
            on_change(now)

    async def _on_tool(self, params) -> None:
        request = getattr(params.context, "pineo_request", None)
        if request is not None:
            lane = {"ticket": self.ticket_router, "status": self.status_router}.get(request.kind) or self.router
            lane.act(request, params.function_name, dict(params.arguments))
        from pipecat.frames.frames import FunctionCallResultProperties
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

    # ---- inputs

    def push_pcm(self, pcm: bytes) -> None:
        """16 kHz mono 16-bit samples. Safe from any thread."""
        if not pcm or self._loop is None:
            return
        frame = InputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            asyncio.ensure_future(self.worker.queue_frame(frame))
        else:
            asyncio.run_coroutine_threadsafe(self.worker.queue_frame(frame), self._loop)

    async def say(self, text: str) -> None:
        """A typed utterance: enters the pipeline as a final transcript, as if Deepgram heard it."""
        from pipecat.utils.time import time_now_iso8601
        await self.worker.queue_frame(TranscriptionFrame(text, "", time_now_iso8601()))

    async def write_ticket(self, bug: dict) -> None:
        await self.ticket_router.write_ticket(bug)

    async def flush_speech(self, why: str = "") -> None:
        """A ticket is closing: make the speech service hand over what it is still holding NOW
        (Deepgram "Finalize"), then tell the hub that nothing more is coming."""
        now = time.monotonic()
        if now - self._flushed_at < 1.5:
            return
        self._flushed_at = now
        asked = False
        connection = getattr(self.stt, "_connection", None) if self.stt is not None else None
        if connection is not None:
            try:
                from deepgram.listen.v1.types import ListenV1Finalize
                self.stt.request_finalize()
                await connection.send_finalize(ListenV1Finalize(type="Finalize"))
                asked = True
            except Exception as problem:
                self.log("voice: could not ask Deepgram to finalize: %s" % str(problem)[:120])
        started = time.monotonic()
        await self.router.flushed(asked)
        if asked:
            self.log("speech flushed%s in %d ms." % ((" (%s)" % why) if why else "", round((time.monotonic() - started) * 1000)))

    def set_listening(self, on: bool) -> None:
        self.mic.listening = bool(on)

    async def wait_idle(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(asyncio.gather(*[r.idle.wait() for r in (self.router, self.ticket_router, self.status_router) if r]), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- running

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.runner.add_workers(*[w for w in (self.worker, self.ticket_worker, self.status_worker) if w])
        self.started.set()
        await self.runner.run()

    async def stop(self) -> None:
        try:
            await self.worker.queue_frame(EndFrame())
            await self.ticket_worker.queue_frame(EndFrame())
            if self.status_worker:
                await self.status_worker.queue_frame(EndFrame())
        except Exception as problem:
            logger.debug("stop: %s" % problem)
