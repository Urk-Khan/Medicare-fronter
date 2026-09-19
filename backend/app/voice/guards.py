"""
Two safety nets that sit in the voice pipeline.

1. SpokenTextGuard (between the LLM and the TTS)
   * Language models occasionally write a tool call out as plain text, e.g.
     `save_qualification({"zip_code": "33101"})`, instead of making the call.
     Without this guard the voice would read that out loud to the lead. The guard
     silently drops such text (the rest of the reply is still spoken).
   * It also notices when the reply contains the Medicare (TPMO) disclaimer, and
     marks it as "protected speech" so the lead can't cut it off halfway.
   * If a second reply to the same caller turn arrives while the disclaimer is still
     being spoken (a turn-detection race), a repeat of the disclaimer is dropped.
   * If the voice connection blips and a reply produces no audio at all, that reply
     is spoken again once, so the caller isn't left in silence.

2. ProtectedSpeechMuteStrategy (inside the user aggregator)
   While protected speech is being spoken, the caller's audio is ignored so a
   stray "uh-huh" or background noise can't interrupt the disclaimer. The lead is
   heard again the moment the AI finishes it.
"""

import asyncio
import re
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy

from app.calls.session import CallSession
from app.core.tasks import spawn

PROTECTED_MAX_SECONDS = 60.0  # never keep the caller muted longer than this


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def disclaimer_key(disclaimer: str, words: int = 6) -> str:
    """The first few words of the disclaimer, normalized — enough to recognise it in a reply."""
    return " ".join(_normalize(disclaimer).split()[:words])


class SpokenTextGuard(FrameProcessor):
    def __init__(self, session: CallSession, tool_names: list[str], disclaimer: str):
        super().__init__()
        self._session = session
        self._tool_prefixes = [f"{name}(" for name in tool_names]
        self._disclaimer_key = disclaimer_key(disclaimer)
        self._duplicate_check = False
        self._duplicate_held: list[LLMTextFrame] = []
        self._last_text = ""        # the most recent thing sent to the voice
        self._last_text_at = 0.0
        self._retried_text = ""
        self._reset()

    def _reset(self):
        self._held: list[Frame] = []   # frames held back while we decide if text is a leaked tool call
        self._held_text = ""
        self._dropping = False
        self._depth = 0
        self._reply_text = ""          # everything spoken so far in this reply

    def _looks_like_tool_call(self, text: str) -> bool | None:
        """True = it's a leaked tool call, None = can't tell yet, False = normal speech."""
        s = text.lstrip().replace(" (", "(")
        if not s:
            return None
        for prefix in self._tool_prefixes:
            if s.startswith(prefix):
                return True
            if prefix.startswith(s):
                return None
        return False

    async def _flush(self):
        held, self._held, self._held_text = self._held, [], ""
        for f in held:
            await self._pass_text(f)

    async def _pass_text(self, frame: LLMTextFrame):
        self._reply_text += frame.text
        if self._disclaimer_key and not self._session.protected_speech_since:
            if self._disclaimer_key in _normalize(self._reply_text):
                self._session.protected_speech_since = time.monotonic()
                logger.debug(f"[call {self._session.call_id[-8:]}] disclaimer started — caller muted until it's read")
        await self.push_frame(frame, FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._duplicate_check:
                self._duplicate_held.append(frame)
                return
            await self._handle_text(frame)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
            # A new reply while the disclaimer is still being spoken (caller muted) can only be a second
            # answer to the same thing the caller said. Hold it until it's complete, then decide.
            self._duplicate_check = bool(self._session.protected_speech_since)
            self._duplicate_held = []
        elif isinstance(frame, ErrorFrame) and "completed with no audio" in str(frame.error):
            spawn(self._respeak_lost_reply, label="re-speak lost reply")
        elif isinstance(frame, TTSSpeakFrame) and direction == FrameDirection.DOWNSTREAM:
            self._remember(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._duplicate_check:
                held, self._duplicate_held, self._duplicate_check = self._duplicate_held, [], False
                text = "".join(f.text for f in held)
                if self._disclaimer_key and self._disclaimer_key in _normalize(text):
                    logger.warning(f"[call {self._session.call_id[-8:]}] dropped a duplicate reply that would have "
                                   f"repeated the disclaimer")
                    held = []
                for f in held:
                    await self._handle_text(f)
            if self._held and self._looks_like_tool_call(self._held_text) is not True:
                await self._flush()
            self._remember(self._reply_text)
            self._reset()
        elif isinstance(frame, InterruptionFrame):
            self._reset()
            self._last_text = ""  # an interrupted reply is supposed to go unheard
            self._duplicate_check, self._duplicate_held = False, []
        await self.push_frame(frame, direction)

    def _remember(self, text: str):
        if text and text.strip():
            self._last_text = text.strip()
            self._last_text_at = time.monotonic()

    async def _respeak_lost_reply(self):
        text = self._last_text
        if not text or text == self._retried_text or time.monotonic() - self._last_text_at > 45:
            return
        self._retried_text = text
        logger.warning(f"[call {self._session.call_id[-8:]}] voice produced no audio for the last reply; "
                       f"saying it again: {text[:60]!r}")
        await asyncio.sleep(0.8)  # let the voice service finish reconnecting
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False), FrameDirection.DOWNSTREAM)

    async def _handle_text(self, frame: LLMTextFrame):
        if self._dropping:
            for ch in frame.text:
                if ch == "(":
                    self._depth += 1
                elif ch == ")":
                    self._depth -= 1
                    if self._depth <= 0:
                        self._dropping = False
                        break
            return
        self._held.append(frame)
        self._held_text += frame.text
        verdict = self._looks_like_tool_call(self._held_text)
        if verdict is None:
            return
        if verdict is False:
            await self._flush()
            return
        # A tool call written out as text: never speak it.
        logger.warning(f"[call {self._session.call_id[-8:]}] dropped tool-call text from the spoken reply: "
                       f"{self._held_text.strip()[:80]!r}")
        text = self._held_text
        self._held, self._held_text = [], ""
        self._depth = 0
        self._dropping = True
        for ch in text[text.find("("):]:
            if ch == "(":
                self._depth += 1
            elif ch == ")":
                self._depth -= 1
                if self._depth <= 0:
                    self._dropping = False
                    break


class ProtectedSpeechMuteStrategy(BaseUserMuteStrategy):
    """Mutes the caller from the moment protected speech is generated until the AI has finished saying it.

    "Finished" = the bot stops speaking after at least ~60% of the disclaimer's expected speaking
    time has passed (so a pause after a short "Great." before the disclaimer doesn't unmute early).
    """

    def __init__(self, session: CallSession, disclaimer: str):
        super().__init__()
        self._session = session
        words = len(_normalize(disclaimer).split())
        self._min_secs = min(PROTECTED_MAX_SECONDS / 2, 0.6 * words / 2.5)

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)
        since = self._session.protected_speech_since
        if not since:
            return False
        elapsed = time.monotonic() - since
        if elapsed > PROTECTED_MAX_SECONDS or (isinstance(frame, BotStoppedSpeakingFrame) and elapsed >= self._min_secs):
            self._session.protected_speech_since = 0.0
            logger.debug(f"[call {self._session.call_id[-8:]}] disclaimer finished — listening to the caller again")
            return False
        return True
