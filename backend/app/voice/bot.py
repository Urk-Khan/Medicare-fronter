"""
Medicare AI agent — Pipecat voice pipeline (pipecat-ai 1.8.1), built on the same
tested structure as the Ashad agent:

    Telnyx audio in -> Cartesia STT -> LLM (OpenAI or Anthropic, with tools)
                    -> Cartesia TTS -> Telnyx audio out

Differences from Ashad, because this is an OUTBOUND platform with its own server:
  * Instead of Pipecat's built-in runner, the Telnyx media WebSocket is served by
    our FastAPI app (app/api/webhooks.py). We still use Pipecat's own Telnyx
    handshake parser and serializer, exactly like the runner does internally.
  * auto_hang_up is off: the platform decides when to hang up, so a successful
    transfer to a closer doesn't get cut off when the AI steps out.
  * The greeting is templated per lead and spoken straight to TTS (Ashad's
    no-LLM-round-trip greeting trick), including the AI + company disclosure.
"""

import asyncio
import time
from datetime import datetime, timezone

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_mute.function_call_user_mute_strategy import FunctionCallUserMuteStrategy
from pipecat.serializers.telnyx import TelnyxFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from app.calls import session as sessions
from app.calls.session import CallSession
from app.config import settings
from app.core.tasks import spawn
from app.db import repo
from app.telephony.telnyx import TELNYX
from app.voice import script as script_mod
from app.voice.guards import ProtectedSpeechMuteStrategy, SpokenTextGuard
from app.voice.tools import MEDICARE_TOOLS

# Ashad's VAD tuning: ignores faint background voices and room noise, snappy turn-taking.
VAD_PARAMS = VADParams(confidence=0.85, min_volume=0.70, start_secs=0.20, stop_secs=0.35)


def warm_vad() -> None:
    """Load the Silero model once at server start (Ashad optimisation) so calls don't pay for it.

    Also makes sure NLTK's 'punkt_tab' sentence data is present. Pipecat needs it to split the
    AI's replies into sentences for the voice; if it's missing and can't be downloaded mid-call,
    the agent goes silent after its greeting. Loading it here surfaces any problem at startup.
    """
    started = time.monotonic()
    SileroVADAnalyzer()
    logger.info(f"Silero VAD warmed in {time.monotonic() - started:.2f}s")
    try:
        import nltk
        from pipecat.utils.string import _sent_tokenizer

        _sent_tokenizer()
        nltk.data.find("tokenizers/punkt_tab")
        logger.info("Sentence tokenizer data (punkt_tab) ready")
    except LookupError:
        logger.error("NLTK 'punkt_tab' data is missing and could not be downloaded. The AI voice will not work. "
                     "Run:  python -m nltk.downloader punkt_tab   (setup.bat does this automatically)")
    except Exception as e:
        logger.warning(f"Could not pre-load the sentence tokenizer: {e}")


# =============================================================================
# Service builders (same providers/settings as Ashad)
# =============================================================================

def build_stt():
    from pipecat.services.cartesia.stt import CartesiaSTTService

    if not settings.cartesia_api_key:
        raise ValueError("CARTESIA_API_KEY is not set in backend/.env")
    return CartesiaSTTService(api_key=settings.cartesia_api_key)


DEFAULT_VOICE_ID = "86e30c1d-714b-4074-a1f2-1cb6b552fb49"


def build_tts(voice_id: str = ""):
    from pipecat.services.cartesia.tts import CartesiaTTSService

    if not settings.cartesia_api_key:
        raise ValueError("CARTESIA_API_KEY is not set in backend/.env")
    voice = (voice_id or "").strip() or settings.cartesia_voice_id or DEFAULT_VOICE_ID
    return CartesiaTTSService(
        api_key=settings.cartesia_api_key,
        settings=CartesiaTTSService.Settings(voice=voice),
    )


def build_llm(system_prompt: str):
    provider = (settings.llm_provider or "openai").strip().lower()
    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in backend/.env")
        return AnthropicLLMService(
            api_key=settings.anthropic_api_key,
            settings=AnthropicLLMService.Settings(
                model=settings.anthropic_model,
                system_instruction=system_prompt,
                enable_prompt_caching=True,
                max_tokens=220,
            ),
        )
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not set in backend/.env")
        return OpenAILLMService(
            api_key=settings.openai_api_key,
            settings=OpenAILLMService.Settings(
                model=settings.openai_model,
                system_instruction=system_prompt,
                # gpt-5.x models need max_completion_tokens (max_tokens is rejected).
                max_completion_tokens=220,
            ),
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r} (use openai or anthropic)")


class SpeechWatcher(FrameProcessor):
    """Lets the rest of the app wait until the bot has finished saying something."""

    def __init__(self, session: CallSession):
        super().__init__()
        self._session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._session.speech_started.set()
            self._session.speech_stopped.clear()
            await self._session.on_bot_started_speaking()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._session.speech_stopped.set()
            spawn(self._session.on_bot_stopped_speaking, label="bot stopped speaking")


# =============================================================================
# One call
# =============================================================================

async def run_call(websocket, call_data, call: dict, lead: dict, runtime: dict, agent: dict) -> None:
    call_id = call["id"]
    call_control_id = call_data["call_id"]
    started = time.monotonic()
    short = call_id[-8:]
    logger.info(f"[call {short}] AI pipeline starting for lead {lead.get('id')} "
                f"(agent {agent.get('name') or 'default'})")

    # Everything the AI says comes from this agent's own script (Agents page).
    values = script_mod.build_values(lead, runtime, agent.get("disclaimer_text") or script_mod.DEFAULT_DISCLAIMER,
                                     agent_name=agent.get("name") or "")
    system_prompt = script_mod.render(agent.get("system_prompt") or script_mod.DEFAULT_SYSTEM_PROMPT, values)
    opening_line = script_mod.render(agent.get("opening_line") or script_mod.DEFAULT_OPENING_LINE, values)

    session = CallSession(call_id=call_id, call_control_id=call_control_id, lead=lead, runtime=runtime,
                          qualification=dict(call.get("qualification") or {}))
    sessions.register(session)

    serializer = TelnyxFrameSerializer(
        stream_id=call_data["stream_id"],
        call_control_id=call_control_id,
        outbound_encoding=call_data.get("outbound_encoding") or "PCMU",
        inbound_encoding="PCMU",
        api_key=settings.telnyx_api_key,
        params=TelnyxFrameSerializer.InputParams(auto_hang_up=False),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = build_stt()
    tts = build_tts(agent.get("voice_id") or "")
    llm = build_llm(system_prompt)

    @stt.event_handler("on_connected")
    async def _stt_connected(_service):
        logger.info(f"[call {short}] Cartesia STT connected ({time.monotonic() - started:.2f}s)")

    @tts.event_handler("on_connected")
    async def _tts_connected(_service):
        logger.info(f"[call {short}] Cartesia TTS connected ({time.monotonic() - started:.2f}s)")

    context = LLMContext(tools=MEDICARE_TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VAD_PARAMS),
            user_turn_stop_timeout=0.35,
            # Don't let the caller cut off the Medicare disclaimer, or talk over a tool that's running.
            user_mute_strategies=[ProtectedSpeechMuteStrategy(session, values["disclaimer"]), FunctionCallUserMuteStrategy()],
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        SpokenTextGuard(session, [fn.__name__ for fn in MEDICARE_TOOLS], values["disclaimer"]),
        tts,
        transport.output(),
        SpeechWatcher(session),
        assistant_aggregator,
    ])

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=8000,  # Telnyx PSTN audio is 8 kHz
            audio_out_sample_rate=8000,
        ),
        app_resources={"session": session},
        idle_timeout_secs=45,  # nobody speaking for 45s -> end the call
    )
    session.worker = worker

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        logger.info(f"[call {short}] media connected ({time.monotonic() - started:.2f}s) — speaking greeting")
        await repo.update_call(call_id, {"status": "AI_CONVERSATION", "ai_state": "greeting"})
        # Straight to TTS (no LLM round-trip) and recorded into the context for later turns.
        await tts.queue_frame(TTSSpeakFrame(opening_line))

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        logger.info(f"[call {short}] media disconnected")
        await worker.cancel()

    async def _max_duration_guard():
        await asyncio.sleep(runtime["max_ai_call_minutes"] * 60)
        if not session.transferred and not session.ending and not session.transfer_started:
            logger.warning(f"[call {short}] reached max AI call length, wrapping up")
            await session.end_after_speech("I'm sorry, I need to wrap up our call now. Thank you so much for your time, and have a great day.")

    guard = asyncio.create_task(_max_duration_guard())
    runner = WorkerRunner(handle_sigint=False)
    try:
        await runner.add_workers(worker)
        await runner.run()
    except Exception as e:
        logger.exception(f"[call {short}] pipeline error: {e}")
    finally:
        guard.cancel()
        sessions.unregister(call_id)

        async def _cleanup():
            await _save_transcript(call_id, context, session)
            if not session.transferred:
                # The AI is done and nobody is bridged in: end the phone call.
                try:
                    await TELNYX.hangup(call_control_id)
                except Exception as e:
                    logger.warning(f"[call {short}] hangup after AI finished failed: {e}")
            logger.info(f"[call {short}] AI pipeline finished after {time.monotonic() - started:.1f}s "
                        f"(transferred={session.transferred}, outcome={session.outcome})")

        # Shielded: if the websocket handler itself is cancelled (server shutdown, abrupt disconnect),
        # the transcript is still saved and the phone leg still cleaned up.
        cleanup = asyncio.ensure_future(_cleanup())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            logger.warning(f"[call {short}] media handler cancelled; finishing cleanup in the background")
            raise


async def _save_transcript(call_id: str, context: LLMContext, session: CallSession) -> None:
    lines = []
    try:
        for message in context.get_messages():
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if role not in ("user", "assistant"):
                continue
            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
            text = (content or "").strip() if isinstance(content, str) else ""
            if text:
                lines.append({"role": "caller" if role == "user" else "agent", "text": text})
    except Exception as e:
        logger.warning(f"could not read transcript for {call_id}: {e}")
    try:
        update = {"transcript": lines, "qualification": session.qualification}
        await repo.update_call(call_id, update)
    except Exception as e:
        logger.warning(f"could not save transcript for {call_id}: {e}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
