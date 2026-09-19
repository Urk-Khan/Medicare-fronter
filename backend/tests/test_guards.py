"""The voice pipeline's safety nets: leaked tool-call text is never spoken; the disclaimer can't be cut off."""
import asyncio
import time

from pipecat.frames.frames import (BotStoppedSpeakingFrame, ErrorFrame, TTSSpeakFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
                                   LLMTextFrame, UserStartedSpeakingFrame)
from pipecat.tests.utils import run_test

from app.calls.session import CallSession
from app.voice.guards import ProtectedSpeechMuteStrategy, SpokenTextGuard
from app.voice.script import DEFAULT_DISCLAIMER

TOOLS = ["save_qualification", "transfer_to_licensed_agent", "schedule_callback", "mark_do_not_call", "end_call"]
DISCLAIMER = "We do not offer every plan available in your area. Currently we represent 7 organizations which offer 42 products in your area."


def _session():
    return CallSession(call_id="00000000-0000-0000-0000-000000000001", call_control_id="v3:x", lead={}, runtime={})


def _spoken(chunks, session=None):
    session = session or _session()
    frames = [LLMFullResponseStartFrame(), *[LLMTextFrame(c) for c in chunks], LLMFullResponseEndFrame()]
    down, _ = asyncio.run(run_test(SpokenTextGuard(session, TOOLS, DISCLAIMER), frames_to_send=frames))
    return "".join(f.text for f in down if isinstance(f, LLMTextFrame)), session


def test_normal_reply_passes_through_unchanged():
    text, _ = _spoken(["Sure", ",", " what", " coverage", " do", " you", " have?"])
    assert text == "Sure, what coverage do you have?"


def test_leaked_tool_call_is_not_spoken():
    text, _ = _spoken(["save", "_qual", "ification", '({"', 'notes": "ok', ' (fine)"', "})"])
    assert text == ""


def test_leaked_tool_call_followed_by_real_speech():
    text, _ = _spoken(['end_call({"outcome":"x"})', " Have a great day."])
    assert text.strip() == "Have a great day."


def test_disclaimer_marks_protected_speech():
    _, session = _spoken(["Great. ", "We do not", " offer every plan available", " in your area."])
    assert session.protected_speech_since > 0
    _, other = _spoken(["What coverage do you have?"])
    assert other.protected_speech_since == 0


def test_mute_strategy_holds_until_disclaimer_spoken():
    async def scenario():
        session = _session()
        strategy = ProtectedSpeechMuteStrategy(session, DEFAULT_DISCLAIMER)
        assert await strategy.process_frame(UserStartedSpeakingFrame()) is False
        session.protected_speech_since = time.monotonic()
        assert await strategy.process_frame(UserStartedSpeakingFrame()) is True
        # A short pause right after it started doesn't unmute.
        assert await strategy.process_frame(BotStoppedSpeakingFrame()) is True
        session.protected_speech_since = time.monotonic() - 30
        assert await strategy.process_frame(BotStoppedSpeakingFrame()) is False
        assert session.protected_speech_since == 0
        assert await strategy.process_frame(UserStartedSpeakingFrame()) is False
    asyncio.run(scenario())


def test_duplicate_reply_during_disclaimer_is_dropped():
    session = _session()
    session.protected_speech_since = time.monotonic()
    text, _ = _spoken(["We do not offer", " every plan available in your area.", " Do you have Part A and B?"], session)
    assert text == ""
    # Something different during the disclaimer still gets through.
    text, _ = _spoken(["Sorry, one moment."], session)
    assert text == "Sorry, one moment."


def test_reply_that_produced_no_audio_is_spoken_again_once():
    async def scenario():
        guard = SpokenTextGuard(_session(), TOOLS, DISCLAIMER)
        pushed = []

        async def capture(frame, direction=None):
            pushed.append(frame)

        guard.push_frame = capture
        guard._remember("What coverage do you have right now?")
        await guard._respeak_lost_reply()
        await guard._respeak_lost_reply()  # never twice for the same reply
        speak = [f for f in pushed if isinstance(f, TTSSpeakFrame)]
        assert len(speak) == 1 and speak[0].text == "What coverage do you have right now?"
        assert speak[0].append_to_context is False
    asyncio.run(scenario())
