"""The Pipecat pipeline with a fake model and no speech service: no network at all."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (FunctionCallFromLLM, FunctionCallsStartedFrame, InputAudioRawFrame, InterimTranscriptionFrame,
                                   LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
                                   TranscriptionFrame)
from pipecat.processors.frame_processor import FrameProcessor

from pineo_band import voice as voice_module
from pineo_band.hub import HubClient
from pineo_band.voice import Voice

KB = json.loads((Path(__file__).parent / "sample_kb.json").read_text())


class FakeLLM(FrameProcessor):
    """Answers each LLMContextFrame from `script(text of the user message)`:
    ("tool", name, args) | ("text", "...") | ("hang",) | ("slow", seconds, name, args)."""

    def __init__(self, script):
        super().__init__()
        self.script, self.handler, self.asked = script, None, []

    def register_function(self, name, handler, **_):
        self.handler = handler

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        body = frame.context.get_messages()[-1]["content"]
        self.asked.append(body)
        answer = self.script(body)
        if answer[0] == "hang":
            return
        if answer[0] == "slow":
            await asyncio.sleep(answer[1])
            answer = ("tool",) + tuple(answer[2:])
        await self.push_frame(LLMFullResponseStartFrame())
        if answer[0] == "text":
            await self.push_frame(LLMTextFrame(answer[1]))
        else:
            call = FunctionCallFromLLM(function_name=answer[1], tool_call_id="c1", arguments=answer[2], context=frame.context)
            await self.push_frame(FunctionCallsStartedFrame(function_calls=[call]))

            async def result(_value, properties=None):
                return None
            await self.handler(SimpleNamespace(function_name=answer[1], arguments=answer[2], context=frame.context, result_callback=result))
        await self.push_frame(LLMFullResponseEndFrame())


def by_transcript(body):
    if "TASK: ticket" in body:
        return ("tool", "write_ticket", {"kind": "bug", "title": "Grey button", "summary": "It stays grey.", "severity": "high",
                                         "elementIds": ["transfer-submit", "nope"], "steps": ["Type 1,5"]})
    if "block my card" in body or "Where is my card" in body:
        return ("tool", "highlight_element", {"elementId": "card-block-toggle", "say": "On Cards, toggle Block card."})
    if "checkbook" in body:
        return ("tool", "no_matching_element", {"say": "There is no checkbook here."})
    if "made up" in body:
        return ("tool", "highlight_element", {"elementId": "not-in-the-app", "say": "There."})
    if "json please" in body:
        return ("text", '<think>hm</think>{"tool": "highlight_element", "elementId": "settings-logout", "say": "In Settings."}')
    if "TASK: command" in body:
        if "file it" in body or "send that now" in body:
            return ("tool", "ticket_command", {"action": "submit"})
        if "scrap that" in body:
            return ("tool", "ticket_command", {"action": "cancel"})
        return ("tool", "ticket_command", {"action": "none"})
    if "report something" in body:
        return ("tool", "open_ticket", {"reason": "wants to report", "feedback": ""})
    if "found a problem" in body:
        return ("tool", "open_ticket", {"reason": "wants to report", "feedback": "the total is wrong"})
    if "wrap up the whole" in body:
        return ("tool", "end_session", {"reason": "done"})
    return ("text", "SKIP")


class Rig:
    def __init__(self, script=by_transcript, ai_close=True, session=True):
        self.lines = []
        self.hub = HubClient("http://127.0.0.1:1", log=self.lines.append)       # never started: send() only records
        self.hub.state.set_kb(KB)
        self.hub.state.app_context = {"screen": "home", "visibleElementIds": ["home-balance"]}
        self.hub.state.session = {"id": "s0", "startedAt": 1} if session else None
        self.llm = FakeLLM(script)
        self.ticket_llm = FakeLLM(script)
        self.status_llm = FakeLLM(lambda body: ("tool", "ticket_status", {"complete": False, "reason": "test default"}))
        self.voice = Voice(self.hub, {}, stt=False, log=self.lines.append, llm=self.llm, ticket_llm=self.ticket_llm,
                           status_llm=self.status_llm, ai_close=ai_close)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.ensure_future(self.voice.run())
        await asyncio.wait_for(self.voice.started.wait(), 3)
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_):
        await self.voice.stop()
        try:
            await asyncio.wait_for(self.task, 3)
        except (asyncio.TimeoutError, Exception):
            self.task.cancel()

    def sent(self, *kinds):
        return [(k, f) for k, f in self.hub.sent if k in kinds]

    async def say(self, text):
        await self.voice.say(text)
        await asyncio.sleep(0.05)
        assert await self.voice.wait_idle(3)
        await asyncio.sleep(0.02)


async def test_question_becomes_assist_and_transcript_is_forwarded():
    async with Rig() as rig:
        await rig.say("where can I block my card?")
        assert rig.sent("transcript") == [("transcript", {"text": "where can I block my card?", "final": True})]
        assert rig.sent("assist") == [("assist", {"question": "where can I block my card?", "elementId": "card-block-toggle",
                                                  "say": "On Cards, toggle Block card."})]
        body = rig.llm.asked[0]
        assert "card-block-toggle | cards | Block card" in body and '"screen": "home"' in body
        timing = rig.voice.router.timings[-1]
        assert timing["tool"] == "highlight_element" and timing["llm_ms"] >= 0 and "total_ms" in timing


async def test_chatter_produces_nothing_but_the_transcript():
    async with Rig() as rig:
        await rig.say("yeah so I told him the pizza was cold")
        assert [k for k, _ in rig.hub.sent] == ["transcript"]
        assert any("not a question for the app" in line for line in rig.lines)


async def test_short_utterances_never_reach_the_model():
    async with Rig() as rig:
        await rig.say("okay")
        await rig.say("uh huh")
        assert rig.llm.asked == [] and [k for k, _ in rig.hub.sent] == ["transcript", "transcript"]


async def test_no_match_and_unknown_element():
    async with Rig() as rig:
        await rig.say("where do I order a checkbook")
        await rig.say("show me the made up thing please")
        none = rig.sent("assist.none")
        assert none[0][1] == {"question": "where do I order a checkbook", "say": "There is no checkbook here."}
        assert "elementId" not in none[1][1] and rig.sent("assist") == []
        # "not found" is never said to room talk, whatever the model does
        await rig.say("my cousin lost his checkbook on the train")
        assert len(rig.sent("assist.none")) == 2 and any("staying silent" in line for line in rig.lines)


async def test_json_fallback_inside_the_pipeline():
    async with Rig() as rig:
        await rig.say("json please where is logout")
        assert rig.sent("assist")[0][1]["elementId"] == "settings-logout"


async def test_feedback_is_only_transcribed_while_a_ticket_is_open_then_write_up_after_submit():
    async with Rig() as rig:
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("this button should be green and why is this so small")
        assert rig.llm.asked == [] and rig.sent("assist", "assist.none") == []
        assert rig.sent("transcript")[0][1]["final"] is True                 # still transcribed for the hub
        bug = {"id": "b1", "screen": "transfer", "transcript": [{"text": "the submit button stays grey"}], "interactions": []}
        rig.hub.state.handle({"type": "bug.submitted", "bugId": "b1", "bug": bug})
        await rig.voice.write_ticket(bug)
        assert await rig.voice.wait_idle(3)
        assert rig.sent("bug.enrich") == [("bug.enrich", {"bugId": "b1", "kind": "bug", "title": "Grey button", "summary": "It stays grey.",
                                                          "severity": "high", "elementIds": ["transfer-submit"], "steps": ["Type 1,5"]})]
        await rig.say("where can I block my card?")                           # and questions work again
        assert len(rig.sent("assist")) == 1
        assert "TASK: assist\n" in rig.llm.asked[-1] and "TASK: ticket" in rig.ticket_llm.asked[0]


async def test_navigation_questions_are_answered_strictly_while_a_ticket_is_open():
    async with Rig() as rig:
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("how do I block my card?")
        assert "TASK: assist-strict" in rig.llm.asked[0]
        assert rig.sent("assist")[0][1]["elementId"] == "card-block-toggle"
        # answered: shown as a caption, but never a final, so it stays out of the ticket
        assert rig.sent("transcript") == [("transcript", {"text": "how do I block my card?", "final": False})]
        await rig.say("where do I order a checkbook")                         # no element: silence, not "not found"
        await rig.say("show me the made up thing please")
        assert rig.sent("assist.none") == [] and len(rig.sent("assist")) == 1
        # not answered: question-shaped feedback goes into the ticket as a final, once
        finals = [f["text"] for _, f in rig.sent("transcript") if f["final"]]
        assert finals == ["where do I order a checkbook", "show me the made up thing please"]


async def test_a_held_question_joins_the_ticket_when_no_answer_comes_in_time(monkeypatch):
    monkeypatch.setattr(voice_module, "HOLD_SECONDS", 0.15)
    def script(body):
        return ("slow", 0.5, "highlight_element", {"elementId": "cards-list", "say": "Here."})
    async with Rig(script) as rig:
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.voice.say("I can't find where the limit is")
        await asyncio.sleep(0.05)
        assert [f["final"] for _, f in rig.sent("transcript")] == [False]
        await asyncio.sleep(0.25)                                             # the bound passed: into the ticket
        assert [f["final"] for _, f in rig.sent("transcript")] == [False, True]
        assert await rig.voice.wait_idle(3)
        assert [f["final"] for _, f in rig.sent("transcript")] == [False, True]   # and not a second time
    async with Rig(lambda body: ("text", "SKIP")) as rig:                     # the model says it is not a question
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("I can't find where the limit is")
        assert [(f["text"], f["final"]) for _, f in rig.sent("transcript")] == [("I can't find where the limit is", False),
                                                                                ("I can't find where the limit is", True)]


async def test_spoken_commands_only_while_a_ticket_is_open_and_only_alone():
    async with Rig() as rig:
        told = []
        rig.voice.router.on_command = told.append
        await rig.say("close this ticket")                                    # nothing open: just speech
        assert rig.sent("gesture") == []
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("I think we should close this ticket after lunch maybe")    # mid-sentence: feedback
        assert rig.sent("gesture") == []
        await rig.say("Okay, let's close this ticket.")
        assert rig.sent("gesture") == [("gesture", {"name": "submit"})] and told == ["submit"]
        assert rig.sent("transcript")[-1][1]["final"] is False                # the command is not part of the ticket
        rig.hub.state.handle({"type": "bug.closing", "bugId": "b1"})
        await rig.say("send it")                                              # already closing: no second submit
        assert len(rig.sent("gesture")) == 1
        rig.hub.state.handle({"type": "bug.submitted", "bugId": "b1"})
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b2"})
        await rig.say("Never mind.")
        assert rig.sent("gesture")[-1] == ("gesture", {"name": "cancel"})


async def test_a_question_does_not_wait_for_a_ticket_write_up():
    def slow_ticket(body):
        return ("slow", 0.6, "write_ticket", {"kind": "bug", "title": "t", "summary": "s", "severity": "low", "elementIds": []})
    async with Rig() as rig:
        rig.ticket_llm.script = slow_ticket
        await rig.voice.write_ticket({"id": "b1", "transcript": [{"text": "x"}]})
        await rig.voice.say("where can I block my card?")
        await asyncio.sleep(0.25)
        assert len(rig.sent("assist")) == 1 and rig.sent("bug.enrich") == []   # answered while the write-up is still running
        assert await rig.voice.wait_idle(3)
        assert len(rig.sent("bug.enrich")) == 1


async def test_ticket_gets_a_plain_write_up_when_the_model_says_nothing_useful():
    async with Rig(lambda body: ("text", "Sorry, I cannot do that.")) as rig:
        await rig.voice.write_ticket({"id": "b7", "elementIds": ["cards-list"], "transcript": [{"text": "the list flickers"}]})
        assert await rig.voice.wait_idle(3)
        fields = rig.sent("bug.enrich")[0][1]
        assert fields["bugId"] == "b7" and fields["title"] == "the list flickers" and fields["severity"] == "medium"
        assert fields["elementIds"] == ["cards-list"]


async def test_a_model_that_never_answers_does_not_block_the_next_question(monkeypatch):
    monkeypatch.setattr(voice_module, "LLM_TIMEOUT", 0.2)
    answers = iter([("hang",), ("tool", "highlight_element", {"elementId": "cards-list", "say": "Here."})])
    async with Rig(lambda body: next(answers)) as rig:
        await rig.voice.say("where are my cards exactly")
        await asyncio.sleep(0.05)
        assert not rig.voice.router.idle.is_set()
        await rig.voice.say("where are my cards I asked")                     # waits behind the stuck one
        assert await rig.voice.wait_idle(3)
        assert [f["question"] for _, f in rig.sent("assist")] == ["where are my cards I asked"]
        assert any("did not answer" in line for line in rig.lines)


async def test_only_the_newest_question_waits_while_the_model_is_busy():
    def script(body):
        return ("slow", 0.25, "highlight_element", {"elementId": "cards-list", "say": "Here."})
    async with Rig(script) as rig:
        for text in ("where are my cards one", "where are my cards two", "where are my cards three"):
            await rig.voice.say(text)
            await asyncio.sleep(0.03)
        assert await rig.voice.wait_idle(3)
        await asyncio.sleep(0.05)
        assert [f["question"] for _, f in rig.sent("assist")] == ["where are my cards one", "where are my cards three"]
        assert len(rig.sent("transcript")) == 3


async def test_not_found_is_not_said_if_a_ticket_was_opened_meanwhile():
    def script(body):
        return ("slow", 0.2, "no_matching_element", {"say": "Not here."})
    # (asked as a navigation question, so only the ticket that opened meanwhile silences it)
    async with Rig(script) as rig:
        await rig.voice.say("where are my cards exactly")
        await asyncio.sleep(0.05)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        assert await rig.voice.wait_idle(3)
        assert rig.sent("assist", "assist.none") == []


async def test_deepgram_pieces_are_joined_and_interims_are_throttled(monkeypatch):
    monkeypatch.setattr(voice_module, "JOIN_SECONDS", 0.15)
    async with Rig() as rig:
        queue = rig.voice.worker.queue_frame
        for text in ("where", "where can", "where can I"):
            await queue(InterimTranscriptionFrame(text, "", "t"))
        await queue(TranscriptionFrame("where can I", "", "t", result=SimpleNamespace(speech_final=False)))
        await asyncio.sleep(0.05)
        await queue(InterimTranscriptionFrame("block my", "", "t"))
        await queue(TranscriptionFrame("block my card?", "", "t", result=SimpleNamespace(speech_final=True)))
        await asyncio.sleep(0.3)
        assert await rig.voice.wait_idle(3)
        finals = [f["text"] for _, f in rig.sent("transcript") if f["final"]]
        interims = [f["text"] for _, f in rig.sent("transcript") if not f["final"]]
        assert finals == ["where can I", "block my card?"]                   # each piece goes to the hub as it comes
        assert 1 <= len(interims) <= 3 and interims[0] == "where"
        assert [f["question"] for _, f in rig.sent("assist")] == ["where can I block my card?"]   # one question to the model
        # a piece with no end-of-speech mark is asked after the join time
        await queue(TranscriptionFrame("where do I order a checkbook", "", "t", result=SimpleNamespace(speech_final=False)))
        await asyncio.sleep(0.4)
        assert await rig.voice.wait_idle(3)
        assert len(rig.sent("assist.none")) == 1


async def test_audio_enters_through_the_pipeline_head_and_listening_gates_it():
    async with Rig() as rig:
        rig.voice.push_pcm(b"\x00\x00" * 320)
        await asyncio.sleep(0.05)
        assert rig.voice.mic.frames == 1
        rig.voice.set_listening(False)
        rig.voice.push_pcm(b"\x00\x00" * 320)
        await asyncio.to_thread(rig.voice.push_pcm, b"\x00\x00" * 320)        # from another thread, like the laptop microphone
        await asyncio.sleep(0.05)
        assert rig.voice.mic.frames == 1
        rig.voice.set_listening(True)
        await asyncio.to_thread(rig.voice.push_pcm, b"\x00\x00" * 320)
        await asyncio.sleep(0.05)
        assert rig.voice.mic.frames == 2


async def test_no_knowledge_base_means_no_model_call():
    async with Rig() as rig:
        rig.hub.state.set_kb([])
        await rig.say("where can I block my card?")
        assert rig.llm.asked == [] and any("no knowledge base" in line for line in rig.lines)


# ---- the AI closes the ticket

def status(complete, reason="clear bug report", delay=0.0):
    def script(body):
        assert "TASK: status" in body
        return ("slow", delay, "ticket_status", {"complete": complete, "reason": reason}) if delay else \
            ("tool", "ticket_status", {"complete": complete, "reason": reason})
    return script


async def test_complete_ticket_is_closed_after_a_quiet_grace(monkeypatch):
    monkeypatch.setattr(voice_module, "GRACE_SECONDS", 0.2)
    async with Rig() as rig:
        rig.status_llm.script = status(True)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("the block card switch moves but the card still shows as active")
        assert rig.sent("ticket.complete") == []                               # not before the grace
        await asyncio.sleep(0.35)
        assert rig.sent("ticket.complete") == [("ticket.complete", {"bugId": "b1", "reason": "clear bug report"})]
        assert "the block card switch moves" in rig.status_llm.asked[0]
        assert any("TICKET COMPLETE (b1)" in line for line in rig.lines) and any("ticket status: COMPLETE" in line and " ms" in line for line in rig.lines)
        await rig.say("one more thing the colour is wrong too")                # already declared complete: not twice
        await asyncio.sleep(0.35)
        assert len(rig.sent("ticket.complete")) == 1


async def test_talking_on_cancels_the_close(monkeypatch):
    monkeypatch.setattr(voice_module, "GRACE_SECONDS", 0.3)
    async with Rig() as rig:
        rig.status_llm.script = status(True)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("the block card switch moves but the card still shows as active")
        await asyncio.sleep(0.1)
        await rig.voice.worker.queue_frame(InterimTranscriptionFrame("and the", "", "t"))     # the person keeps talking
        await asyncio.sleep(0.4)
        assert rig.sent("ticket.complete") == [] and any("still talking" in line for line in rig.lines)
        rig.status_llm.script = status(False, "trailing off")
        await rig.say("and the label is also wrong I think")
        await asyncio.sleep(0.4)
        assert rig.sent("ticket.complete") == [] and len(rig.status_llm.asked) == 2


async def test_a_verdict_that_arrives_after_more_speech_is_ignored(monkeypatch):
    monkeypatch.setattr(voice_module, "GRACE_SECONDS", 0.1)
    async with Rig() as rig:
        rig.status_llm.script = status(True, delay=0.3)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.voice.say("the block card switch moves but the card still shows as active")
        await asyncio.sleep(0.1)
        await rig.voice.worker.queue_frame(InterimTranscriptionFrame("wait", "", "t"))
        await asyncio.sleep(0.5)
        assert rig.sent("ticket.complete") == [] and any("more was said meanwhile" in line for line in rig.lines)


async def test_no_completeness_call_for_fragments_questions_or_without_a_ticket(monkeypatch):
    monkeypatch.setattr(voice_module, "GRACE_SECONDS", 0.05)
    async with Rig() as rig:
        rig.status_llm.script = status(True)
        await rig.say("the block card switch is broken and it annoys me")      # no ticket open
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("so the thing is")                                       # trailing off
        await rig.say("how do I block my card?")                               # a question to the assistant, answered
        await asyncio.sleep(0.2)
        assert rig.status_llm.asked == [] and rig.sent("ticket.complete") == []
        rig.hub.state.handle({"type": "bug.closing", "bugId": "b1"})           # closing already: nothing to decide
        await rig.say("the total on this screen is clearly wrong")
        await asyncio.sleep(0.2)
        assert rig.status_llm.asked == []


async def test_ticket_closed_elsewhere_during_the_grace_sends_nothing(monkeypatch):
    monkeypatch.setattr(voice_module, "GRACE_SECONDS", 0.2)
    async with Rig() as rig:
        rig.status_llm.script = status(True)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("the block card switch moves but the card still shows as active")
        rig.hub.state.handle({"type": "bug.submitted", "bugId": "b1"})         # the single rotation got there first
        await asyncio.sleep(0.35)
        assert rig.sent("ticket.complete") == []


async def test_next_issue_command():
    async with Rig() as rig:
        rig.hub.state.session = None
        await rig.say("Next issue.")                                          # no session: just speech
        assert rig.sent("gesture") == []
        rig.hub.state.handle({"type": "session.started", "sessionId": "s1"})
        await rig.say("Okay, next issue please.")                             # session, no ticket: still valid
        assert rig.sent("gesture") == [("gesture", {"name": "next"})]
        assert rig.sent("transcript")[-1][1] == {"text": "Okay, next issue please.", "final": False}
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("This button should be green. Next issue.")
        finals = [f["text"] for _, f in rig.sent("transcript") if f["final"]]
        assert finals[-1] == "This button should be green."                   # belongs to the current ticket
        assert rig.sent("transcript")[-1][1] == {"text": "Next issue.", "final": False}
        assert len(rig.sent("gesture")) == 2 and rig.hub.sent[-1] == ("gesture", {"name": "next"})   # after the transcript
        await rig.say("the next ticket I file will be about the colours")     # mid-sentence: nothing
        await rig.say("I will look at the next issue")                        # no sentence mark before it: nothing
        assert len(rig.sent("gesture")) == 2
        # as its own piece after an earlier piece of the same utterance
        queue = rig.voice.worker.queue_frame
        await queue(TranscriptionFrame("the label is cut off", "", "t", result=SimpleNamespace(speech_final=False)))
        await queue(TranscriptionFrame("Next bug.", "", "t", result=SimpleNamespace(speech_final=True)))
        await asyncio.sleep(0.1)
        assert rig.sent("speech")[0] == ("speech", {"active": True})
        assert len(rig.sent("gesture")) == 3
        assert [f["text"] for _, f in rig.sent("transcript") if f["final"]][-1] == "the label is cut off"


async def test_ai_close_is_off_by_default_and_costs_nothing():
    async with Rig(ai_close=False) as rig:
        assert rig.voice.watcher is None and rig.voice.status_router is None
        assert rig.voice.router.on_speech is None and rig.voice.router.on_feedback is None
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("the block card switch moves but the card still shows as active")
        await asyncio.sleep(0.1)
        assert rig.status_llm.asked == [] and rig.sent("ticket.complete") == []
    assert Voice(HubClient("http://127.0.0.1:1", log=print), {}, stt=False, llm=FakeLLM(None), ticket_llm=FakeLLM(None)).ai_close is False


# ---- speech in progress, flushing, and late pieces of a sentence

def live(text, **more):
    return TranscriptionFrame(text, "", "t", result=SimpleNamespace(speech_final=False, **more))


async def test_speech_activity_is_reported(monkeypatch):
    monkeypatch.setattr(voice_module, "QUIET_SECONDS", 0.15)
    monkeypatch.setattr(voice_module, "JOIN_SECONDS", 0.05)
    async with Rig() as rig:
        queue = rig.voice.worker.queue_frame
        await queue(InterimTranscriptionFrame("I want", "", "t", result=SimpleNamespace()))
        await queue(InterimTranscriptionFrame("I want balance", "", "t", result=SimpleNamespace()))
        await asyncio.sleep(0.05)
        assert rig.sent("speech") == [("speech", {"active": True})] and rig.voice.router.speech_active
        await queue(live("I want balance numbers"))
        await asyncio.sleep(0.1)
        assert len(rig.sent("speech")) == 1                                   # still within the quiet time
        await asyncio.sleep(0.2)
        assert rig.sent("speech") == [("speech", {"active": True}), ("speech", {"active": False})]
        await rig.say("typed utterances are whole at once")                   # no speech messages for typed text
        assert len(rig.sent("speech")) == 2


async def test_flush_on_closing_joins_the_pieces_and_says_nothing_more_is_coming(monkeypatch):
    monkeypatch.setattr(voice_module, "FLUSH_SECONDS", 0.3)
    monkeypatch.setattr(voice_module, "JOIN_SECONDS", 5)
    async with Rig() as rig:
        queue = rig.voice.worker.queue_frame
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await queue(live("I want balance numbers"))
        await asyncio.sleep(0.05)
        rig.hub.state.handle({"type": "bug.closing", "bugId": "b1"})
        waiter = asyncio.ensure_future(rig.voice.router.flushed(True))        # Finalize was sent to the speech service
        await asyncio.sleep(0.05)
        late = TranscriptionFrame("to be red.", "", "t", result=SimpleNamespace(speech_final=True), finalized=True)
        await queue(late)
        await asyncio.wait_for(waiter, 1)
        finals = [f["text"] for _, f in rig.sent("transcript") if f["final"]]
        assert finals == ["I want balance numbers", "to be red."]             # both reach the hub before...
        assert rig.hub.sent[-1] == ("speech", {"active": False, "flushed": True})   # ...the hub is told it is over
        assert rig.llm.asked == []                                            # and the fragment was never taken for a question
        # nothing pending and no speech service: still answers at once
        await rig.voice.flush_speech("test")
        assert rig.hub.sent[-1] == ("speech", {"active": False, "flushed": True})


async def test_the_rest_of_a_sentence_after_the_ticket_closed_goes_to_that_ticket(monkeypatch):
    monkeypatch.setattr(voice_module, "JOIN_SECONDS", 0.05)
    async with Rig() as rig:
        queue = rig.voice.worker.queue_frame
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        rig.hub.state.handle({"type": "bug.submitted", "bugId": "b1"})
        rig.voice.router.ticket_closed("b1")
        await queue(live("to look red."))
        await queue(live("on the home screen"))                               # short and lower-case: also a continuation
        await asyncio.sleep(0.2)
        assert rig.llm.asked == [] and rig.sent("assist") == []
        assert rig.sent("transcript")[-2:] == [("transcript", {"text": "to look red.", "final": True, "bugId": "b1"}),
                                               ("transcript", {"text": "on the home screen", "final": True, "bugId": "b1"})]
        for filler in ("Okay.", "Thanks.", "Yeah, cool."):                    # never glued onto the closed ticket
            await queue(live(filler))
            await asyncio.sleep(0.12)
        await queue(live("Where is my card?"))                                # a short question right after the close is answered
        await asyncio.sleep(0.3)
        assert await rig.voice.wait_idle(3)
        assert not any(f.get("bugId") for _, f in rig.sent("transcript")[-4:])
        assert rig.sent("assist")[-1][1]["question"] == "Where is my card?"
        await queue(live("Where can I block my card?"))                       # a real, new question is still answered
        await asyncio.sleep(0.2)
        assert await rig.voice.wait_idle(3) and len(rig.sent("assist")) == 2
        rig.voice.router.closed_at -= 10                                      # long after the close: ordinary speech again
        await queue(live("to look red."))
        await asyncio.sleep(0.2)
        assert "bugId" not in rig.sent("transcript")[-1][1]


async def test_a_continuation_during_closing_is_never_asked_as_a_question(monkeypatch):
    monkeypatch.setattr(voice_module, "JOIN_SECONDS", 0.05)
    async with Rig() as rig:
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        rig.hub.state.handle({"type": "bug.closing", "bugId": "b1"})
        await rig.voice.worker.queue_frame(live("to look red on the home screen"))
        await asyncio.sleep(0.2)
        assert rig.llm.asked == []
        assert [f["text"] for _, f in rig.sent("transcript") if f["final"]] == ["to look red on the home screen"]


async def test_final_lag_is_measured_from_the_audio_clock():
    async with Rig() as rig:
        rig.voice.router.audio_seconds = lambda: 12.0
        await rig.voice.worker.queue_frame(live("the total is wrong on this screen", start=9.0, duration=2.5))
        await asyncio.sleep(0.05)
        assert rig.voice.router.lags == [500] and any("final after 500 ms" in line for line in rig.lines)


async def test_questions_are_answered_only_during_a_session():
    async with Rig(session=False) as rig:
        await rig.say("where can I block my card?")
        assert rig.llm.asked == [] and rig.sent("assist") == [] and any("no session is active" in line for line in rig.lines)
        rig.hub.state.handle({"type": "session.started", "sessionId": "s1"})
        await rig.say("where can I block my card?")
        assert len(rig.sent("assist")) == 1
        rig.hub.state.handle({"type": "session.ended", "sessionId": "s1", "tickets": 0})
        await rig.say("where can I block my card?")
        assert len(rig.sent("assist")) == 1


async def test_speech_service_state_is_reported_and_never_gives_up():
    async with Rig() as rig:
        assert rig.voice.stt_state is None                                    # typed mode: no speech service
        rig.voice.stt = SimpleNamespace(_connection=object())
        changes = []
        task = asyncio.ensure_future(rig.voice.watch_stt(changes.append, every=0.02))
        await asyncio.sleep(0.06)
        rig.voice.stt._connection = None
        await asyncio.sleep(0.06)
        rig.voice.stt._connection = object()
        await asyncio.sleep(0.06)
        task.cancel()
        assert changes == ["reconnecting", "ok"]
        rig.voice.stt = SimpleNamespace(_connection=None)                     # still connecting when the watch starts
        task = asyncio.ensure_future(rig.voice.watch_stt(changes.append, every=0.02))
        await asyncio.sleep(0.05)
        rig.voice.stt._connection = object()
        await asyncio.sleep(0.06)
        task.cancel()
        assert changes == ["reconnecting", "ok", "ok"]                        # the dashboard is told once it is up
        assert any("SPEECH SERVICE DOWN" in line for line in rig.lines) and any("speech service back" in line for line in rig.lines)
        rig.voice.stt = None
    hub = HubClient("http://127.0.0.1:1", log=lambda t: None)
    voice = Voice(hub, {}, deepgram_key="x", stt=True, llm=FakeLLM(None), ticket_llm=FakeLLM(None))
    tracker = voice.stt._quick_failure_tracker
    assert not any(tracker.record(0.0).should_give_up for _ in range(50)) and tracker.count <= 4


async def test_a_late_response_finishes_its_own_request_not_the_newer_one(monkeypatch):
    monkeypatch.setattr(voice_module, "LLM_TIMEOUT", 0.15)
    answers = iter([("slow", 0.5, "highlight_element", {"elementId": "cards-list", "say": "Old."}),
                    ("slow", 0.3, "highlight_element", {"elementId": "card-block-toggle", "say": "New."})])
    async with Rig(lambda body: next(answers)) as rig:
        await rig.voice.say("where are my cards exactly")                    # hangs past the watchdog
        await asyncio.sleep(0.25)
        monkeypatch.setattr(voice_module, "LLM_TIMEOUT", 5)
        await rig.voice.say("how do I block my card now")                    # dispatched while the old call still runs
        await asyncio.sleep(0.4)                                             # the OLD response ends here
        newer = rig.voice.router.current
        assert newer is not None and newer.text == "how do I block my card now" and not newer.done
        assert await rig.voice.wait_idle(3)
        assert rig.sent("assist")[-1][1] == {"question": "how do I block my card now", "elementId": "card-block-toggle", "say": "New."}


async def test_the_model_client_has_a_short_timeout_and_no_retries():
    hub = HubClient("http://127.0.0.1:1", log=lambda t: None)
    voice = Voice(hub, {"key": "k", "base_url": "https://example.invalid/v1", "model": "m"}, stt=False)
    for service in (voice.llm, voice.ticket_llm):
        assert service._client.timeout == voice_module.LLM_CLIENT_TIMEOUT and service._client.max_retries == 0


# ---- tickets and the session by voice

def gestures(rig):
    return [f["name"] for _, f in rig.sent("gesture")]


async def test_open_a_ticket_by_voice():
    async with Rig() as rig:
        await rig.say("Open a ticket.")                                       # exact phrase: no model call
        assert gestures(rig) == ["open"] and rig.llm.asked == []
        assert rig.sent("transcript")[-1][1]["final"] is False
        assert any("VOICE COMMAND OPEN (phrase" in line for line in rig.lines)
        await rig.say("I want to report something")                           # natural wording: the model decides
        assert gestures(rig) == ["open", "open"] and any("VOICE COMMAND OPEN (model" in line for line in rig.lines)
        assert rig.voice.router.timings[-1]["source"] == "model"
        rig.hub.state.session = None
        await rig.say("Open a ticket.")                                       # no session: nothing
        assert len(gestures(rig)) == 2


async def test_open_with_the_report_in_the_same_breath():
    async with Rig() as rig:
        await rig.voice.say("Open a ticket: the submit button is too small")
        await asyncio.sleep(0.1)
        assert gestures(rig) == ["open"]
        assert not any(f["final"] for _, f in rig.sent("transcript"))         # nothing filed before the ticket exists
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})           # the hub opens it
        await asyncio.sleep(0.15)
        assert rig.sent("transcript")[-1][1] == {"text": "the submit button is too small", "final": True}
    async with Rig() as rig:                                                  # the same through the model
        await rig.voice.say("hey I found a problem the total is wrong")
        await asyncio.sleep(0.15)
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b2"})
        await asyncio.sleep(0.15)
        assert gestures(rig) == ["open"] and rig.sent("transcript")[-1][1] == {"text": "the total is wrong", "final": True}


async def test_submit_and_cancel_by_natural_wording_only_for_short_instruction_like_finals():
    async with Rig() as rig:
        rig.hub.state.handle({"type": "bug.opened", "bugId": "b1"})
        await rig.say("the close button is too small")                        # mentions "close": the model says none
        assert gestures(rig) == [] and "TASK: command" in rig.llm.asked[-1]
        assert [(f["text"], f["final"]) for _, f in rig.sent("transcript")] == [("the close button is too small", False),
                                                                                ("the close button is too small", True)]
        asked = len(rig.llm.asked)
        await rig.say("the close button on this screen is far too small and I keep missing it")   # long: never asked
        await rig.say("make the title a bit bigger")                          # short but plainly feedback: never asked
        assert len(rig.llm.asked) == asked and gestures(rig) == []
        await rig.say("okay you can send that now")
        assert gestures(rig) == ["submit"] and any("VOICE COMMAND SUBMIT (model" in line for line in rig.lines)
        assert rig.sent("transcript")[-1][1] == {"text": "okay you can send that now", "final": False}   # never in the ticket
        await rig.say("scrap that one")
        assert gestures(rig) == ["submit", "cancel"]
        await rig.say("That's it.")                                           # the phrase fast path
        assert gestures(rig)[-1] == "submit" and any("VOICE COMMAND SUBMIT (phrase" in line for line in rig.lines)
        rig.hub.state.handle({"type": "bug.closing", "bugId": "b1"})
        await rig.say("go ahead and file it")                                 # already closing: no second submit
        assert len(gestures(rig)) == 3


async def test_end_the_session_by_voice():
    async with Rig() as rig:
        await rig.say("End the session.")
        assert gestures(rig) == ["double"] and any("VOICE COMMAND END (phrase" in line for line in rig.lines)
        await rig.say("okay let us wrap up the whole thing now")
        assert gestures(rig) == ["double", "double"]
        rig.hub.state.session = None
        await rig.say("End the session.")
        assert len(gestures(rig)) == 2
