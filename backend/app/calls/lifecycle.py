"""
Telnyx webhook event handling — the call state machine.

Lead leg (the Medicare lead):
  call.initiated        -> RINGING
  call.answered         -> start recording; wait for voicemail detection (if on) or start the AI
  call.machine.detection.ended
                        -> machine: hang up (outcome voicemail) / human or not_sure: start the AI
  call.recording.saved  -> store recording id/url
  call.hangup           -> finalise the call, free closers, update the lead (retry/callback/final)

Closer leg (a licensed agent being rung for a transfer): routed to app/calls/transfer.py.
"""

import asyncio
import secrets
from datetime import datetime, timezone

from loguru import logger

from app.calls import outcomes, session as sessions, transfer
from app.config import settings
from app.core.runtime import get_runtime
from app.core.tasks import spawn
from app.db import repo
from app.telephony.telnyx import TELNYX, decode_state

AMD_FALLBACK_SECONDS = 6.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


async def handle_event(event: dict) -> None:
    event_type = event.get("event_type") or ""
    payload = event.get("payload") or {}
    ccid = payload.get("call_control_id")
    state = decode_state(payload.get("client_state"))

    # ---- closer legs -------------------------------------------------------
    if state.get("k") == "closer":
        attempt_id = state.get("a")
        call_id = state.get("c")
        await _record_event(event, call_id)
        if not attempt_id:
            return
        if event_type == "call.answered":
            await transfer.on_closer_answered(attempt_id, ccid)
        elif event_type == "call.gather.ended":
            await transfer.on_closer_gather_ended(attempt_id, payload.get("digits"))
        elif event_type == "call.bridged":
            await transfer.on_closer_bridged(attempt_id)
        elif event_type == "call.hangup":
            await transfer.on_closer_hangup(attempt_id, payload.get("hangup_cause"))
        return

    # ---- lead legs ---------------------------------------------------------
    call = None
    if state.get("k") == "lead" and state.get("c"):
        call = await repo.get_call(state["c"])
    if not call and ccid:
        call = await repo.get_call_by_ccid(ccid)
    if not call:
        logger.debug(f"webhook {event_type} for unknown call ccid={ccid}")
        return
    if not await _record_event(event, call["id"]):
        return  # duplicate delivery

    call_id = call["id"]
    if ccid and not call.get("telnyx_call_control_id"):
        await repo.update_call(call_id, {"telnyx_call_control_id": ccid,
                                         "telnyx_call_session_id": payload.get("call_session_id")})
        call["telnyx_call_control_id"] = ccid

    if event_type == "call.initiated":
        if call["status"] == "DIALING":
            await repo.update_call(call_id, {"status": "RINGING"})

    elif event_type == "call.answered":
        await _on_answered(call, event)

    elif event_type in ("call.machine.detection.ended", "call.machine.premium.detection.ended"):
        await _on_machine_detection(call, payload.get("result"))

    elif event_type == "call.recording.saved":
        urls = payload.get("recording_urls") or payload.get("public_recording_urls") or {}
        await repo.update_call(call_id, {
            "recording_id": payload.get("recording_id"),
            "recording_url": urls.get("mp3") or urls.get("wav"),
        })

    elif event_type == "call.streaming.failed":
        logger.error(f"[call {call_id[-8:]}] media stream failed: {payload}")
        await outcomes.set_outcome(call_id, "failed")
        await TELNYX.hangup(call["telnyx_call_control_id"])

    elif event_type == "call.hangup":
        await finalize_call(call_id, payload.get("hangup_cause"), event.get("occurred_at"))


async def _record_event(event: dict, call_id: str | None) -> bool:
    event_id = event.get("id")
    if not event_id:
        return True
    payload = dict(event.get("payload") or {})
    payload.pop("client_state", None)
    try:
        return await repo.insert_call_event({
            "event_id": event_id,
            "call_id": call_id,
            "call_control_id": payload.get("call_control_id"),
            "event_type": event.get("event_type"),
            "payload": payload,
        })
    except Exception as e:
        logger.warning(f"could not store webhook event: {e}")
        return True


async def _on_answered(call: dict, event: dict) -> None:
    call_id = call["id"]
    ccid = call["telnyx_call_control_id"]
    runtime = await get_runtime()
    moved = await repo.transition_call(call_id, ["DIALING", "RINGING"],
                                       {"status": "ANSWERED", "answered_at": event.get("occurred_at") or _now()})
    if not moved:
        return
    await TELNYX.start_recording(ccid)
    if runtime["amd_mode"] == "disabled":
        await start_ai(call_id)
    else:
        # Voicemail detection result normally arrives within ~3.5s; don't wait forever.
        spawn(_amd_fallback, call_id, label=f"call {call_id[-8:]} voicemail-detection fallback")


async def _amd_fallback(call_id: str) -> None:
    await asyncio.sleep(AMD_FALLBACK_SECONDS)
    call = await repo.get_call(call_id)
    if call and call["status"] == "ANSWERED" and not call.get("amd_result"):
        logger.info(f"[call {call_id[-8:]}] no voicemail-detection result yet, starting AI")
        await start_ai(call_id)


async def _on_machine_detection(call: dict, result: str | None) -> None:
    call_id = call["id"]
    result = (result or "not_sure").lower()
    await repo.update_call(call_id, {"amd_result": "machine" if result.startswith("machine") or result.startswith("fax") else result})
    if call["status"] != "ANSWERED":
        return
    if result.startswith("machine") or result.startswith("fax"):
        logger.info(f"[call {call_id[-8:]}] voicemail detected -> hanging up")
        await outcomes.set_outcome(call_id, "voicemail")
        await TELNYX.hangup(call["telnyx_call_control_id"])
    else:
        await start_ai(call_id)


async def start_ai(call_id: str) -> None:
    """Ask Telnyx to open the bidirectional media stream; the AI pipeline starts when it connects."""
    token = secrets.token_urlsafe(24)
    call = await repo.transition_call(call_id, ["ANSWERED"], {"status": "AI_CONVERSATION", "ai_state": "connecting", "stream_token": token})
    if not call:
        return  # already started by another event, or the call ended
    url = f"{settings.public_ws_base_url}/ws/telnyx/media?call_id={call_id}&token={token}"
    try:
        await TELNYX.start_stream(call["telnyx_call_control_id"], url)
        logger.info(f"[call {call_id[-8:]}] media stream requested")
    except Exception as e:
        logger.error(f"[call {call_id[-8:]}] could not start media stream: {e}")
        await outcomes.set_outcome(call_id, "failed")
        await TELNYX.hangup(call["telnyx_call_control_id"])


async def finalize_call(call_id: str, hangup_cause: str | None = None, occurred_at: str | None = None) -> None:
    """Idempotently close out a call: timings, outcome, closers, lead status."""
    call = await repo.get_call(call_id)
    if not call or call.get("ended_at"):
        return
    ended = _ts(occurred_at) or datetime.now(timezone.utc)
    answered = _ts(call.get("answered_at"))
    outcome = outcomes.outcome_from_hangup(call, hangup_cause)
    started = _ts(call.get("started_at"))
    duration = int((ended - answered).total_seconds()) if answered else 0
    total = int((ended - started).total_seconds()) if started else duration
    await repo.update_call(call_id, {
        "status": "ENDED",
        "ai_state": None,
        "ended_at": ended.isoformat(),
        "duration_seconds": max(0, total),
        "talk_seconds": max(0, duration),
        "hangup_cause": hangup_cause,
        "outcome": outcome,
    })
    await transfer.on_lead_hangup(call_id)
    live = sessions.get(call_id)
    if live:
        await live.stop_now()
    call["outcome"] = outcome
    try:
        await outcomes.apply_lead_disposition(call, outcome)
    except Exception as e:
        logger.exception(f"lead disposition failed for call {call_id}: {e}")
    logger.info(f"[call {call_id[-8:]}] ended: outcome={outcome} cause={hangup_cause} duration={duration}s")
