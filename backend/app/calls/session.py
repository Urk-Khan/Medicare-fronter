"""
In-memory state for each call that currently has a live AI conversation.

The database is the source of truth for everything that must survive a restart
(call rows, leads, callbacks, transfer attempts). This registry only holds what
can't live in a database: the running Pipecat worker, so other parts of the app
(webhooks, the transfer engine) can make the AI say something or stop it.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame

from app.core.tasks import spawn


@dataclass
class CallSession:
    call_id: str
    call_control_id: str
    lead: dict
    runtime: dict
    worker: Any = None  # pipecat PipelineWorker
    speech_started: asyncio.Event = field(default_factory=asyncio.Event)
    speech_stopped: asyncio.Event = field(default_factory=asyncio.Event)
    qualification: dict = field(default_factory=dict)
    transfer_started: bool = False
    transferred: bool = False  # lead is bridged to a closer: the AI must not hang up
    ending: bool = False
    outcome: str | None = None
    auto_end_armed: bool = False
    spoke_after_arm: bool = False
    protected_speech_since: float = 0.0  # >0 while the disclaimer is being spoken (caller muted)

    def arm_auto_end(self, fallback_seconds: float = 15.0) -> None:
        """End the call as soon as the AI finishes its next spoken reply (the goodbye).

        Used after a callback is booked or a do-not-call request, so the call never hangs
        open if the model forgets to call end_call.
        """
        if self.auto_end_armed:
            return
        self.auto_end_armed = True
        self.spoke_after_arm = False

        async def _fallback():
            await asyncio.sleep(fallback_seconds)
            if not self.ending and not self.transfer_started:
                await self.end_after_speech()

        spawn(_fallback, label=f"call {self.call_id[-8:]} auto-end fallback")

    async def on_bot_started_speaking(self) -> None:
        if self.auto_end_armed:
            self.spoke_after_arm = True

    async def on_bot_stopped_speaking(self) -> None:
        if self.auto_end_armed and self.spoke_after_arm and not self.ending and not self.transfer_started:
            await asyncio.sleep(0.8)
            await self.end_after_speech()

    async def say(self, text: str) -> None:
        if self.worker is None:
            return
        await self.worker.queue_frame(TTSSpeakFrame(text))

    async def say_and_wait(self, text: str, max_wait: float = 15.0) -> None:
        """Speak and wait (best effort) until the bot has finished saying it."""
        if self.worker is None:
            return
        self.speech_started.clear()
        self.speech_stopped.clear()
        await self.say(text)
        try:
            await asyncio.wait_for(self.speech_started.wait(), timeout=4.0)
            await asyncio.wait_for(self.speech_stopped.wait(), timeout=max_wait)
        except asyncio.TimeoutError:
            # Fall back to a length-based estimate if speaking events didn't arrive.
            await asyncio.sleep(min(6.0, 0.35 * len(text.split())))

    async def end_after_speech(self, text: str | None = None) -> None:
        """Queue an optional goodbye then end the pipeline once it has been spoken."""
        if self.worker is None or self.ending:
            return
        self.ending = True
        frames = [TTSSpeakFrame(text)] if text else []
        frames.append(EndWorkerFrame())
        await self.worker.queue_frames(frames)

    async def stop_now(self) -> None:
        if self.worker is not None:
            try:
                await self.worker.cancel()
            except Exception as e:
                logger.debug(f"worker cancel ignored: {e}")


_SESSIONS: dict[str, CallSession] = {}


def register(session: CallSession) -> None:
    _SESSIONS[session.call_id] = session


def unregister(call_id: str) -> None:
    _SESSIONS.pop(call_id, None)


def get(call_id: str) -> CallSession | None:
    return _SESSIONS.get(call_id)


def active_ids() -> list[str]:
    return list(_SESSIONS.keys())
