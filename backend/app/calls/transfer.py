"""
Warm transfer to licensed closers, with automatic failover.

When the AI calls transfer_to_licensed_agent:

  1. Pick the best closer who is enabled + marked Available + not already ringing
     or on a call (priority order, then least-recently-assigned for fair rotation)
     and reserve them atomically so two calls can never grab the same person.
  2. Dial that closer on a separate call leg. The lead stays on the line with the AI.
  3. When the closer picks up, a short whisper tells them who's waiting and asks them
     to press 1 (optional, Settings -> "Closer must press 1"). This also stops a
     closer's voicemail from "accepting" the transfer.
  4. Accepted -> the AI says a short handoff line, the two legs are bridged, the AI
     steps out. The call keeps recording.
  5. Busy, no answer, voicemail, declined, or any error -> that closer is released
     and the next available closer is tried, automatically.
  6. Nobody available -> a callback is booked and the AI tells the lead a licensed
     agent will call them back, then says goodbye.

Every attempt is stored in transfer_attempts so the Call Details page shows exactly
who was tried and what happened.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.calls import outcomes, session as sessions
from app.core.phones import spoken_digits
from app.core.runtime import get_runtime
from app.core.tasks import spawn
from app.db import repo
from app.telephony.telnyx import TELNYX, TelnyxError

OPEN_ATTEMPT_STATUSES = {"ringing", "answered", "connecting"}
RINGING_STATUSES = {"ringing", "answered"}


@dataclass
class TransferState:
    call_id: str
    tried: list[str] = field(default_factory=list)
    done: bool = False
    current_attempt_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_STATES: dict[str, TransferState] = {}


def is_transferring(call_id: str) -> bool:
    state = _STATES.get(call_id)
    return bool(state and not state.done)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Entry point (called by the AI tool)
# =============================================================================

async def start_transfer(call_id: str) -> str:
    if call_id in _STATES:
        return "already_in_progress"
    _STATES[call_id] = TransferState(call_id=call_id)
    await repo.update_call(call_id, {"status": "TRANSFERRING", "ai_state": "transferring"})
    spawn(_try_next, call_id, label=f"transfer {call_id[-8:]} next closer", retries=2)
    return "connecting"


async def _try_next(call_id: str) -> None:
    state = _STATES.get(call_id)
    if not state:
        return
    async with state.lock:
        if state.done:
            return
        call = await repo.get_call(call_id)
        if not call or call.get("status") == "ENDED":
            state.done = True
            return
        runtime = await get_runtime()

        closer = None
        for candidate in await repo.available_closers(exclude_ids=state.tried):
            state.tried.append(candidate["id"])
            if await repo.reserve_closer(candidate["id"], call_id):
                closer = candidate
                break
        if closer is None:
            state.done = True
            spawn(_all_closers_unavailable, call_id, label=f"transfer {call_id[-8:]} no closer", retries=2)
            return

        try:
            attempt = await repo.insert_attempt({
                "call_id": call_id,
                "closer_id": closer["id"],
                "closer_name": closer["name"],
                "destination": closer["destination"],
                "status": "ringing",
            })
        except Exception:
            # Don't leave the closer marked busy if we couldn't even record the attempt; allow a retry.
            state.tried.remove(closer["id"])
            await repo.release_closer(closer["id"], only_if_call_id=call_id)
            raise
        state.current_attempt_id = attempt["id"]
        logger.info(f"[transfer {call_id[-8:]}] trying closer {closer['name']} ({closer['destination']})")
        try:
            leg = await TELNYX.dial(
                to=closer["destination"],
                client_state={"k": "closer", "c": call_id, "a": attempt["id"]},
                timeout_secs=runtime["closer_ring_seconds"],
                amd_mode="disabled",
                time_limit_secs=4 * 3600,
            )
            await repo.update_attempt(attempt["id"], {"leg_control_id": leg.get("call_control_id")})
        except Exception as e:
            logger.warning(f"[transfer {call_id[-8:]}] dialing {closer['name']} failed: {e}")
            await _close_attempt(attempt["id"], "failed", f"dial_error: {str(e)[:120]}", hang_up_leg=False, try_next=False)
            spawn(_try_next, call_id, label=f"transfer {call_id[-8:]} next closer", retries=2)
            return

        watchdog_secs = runtime["closer_ring_seconds"] + (15 if runtime["closer_require_accept"] else 0) + 10
        spawn(_watchdog, attempt["id"], watchdog_secs, label=f"transfer {call_id[-8:]} watchdog")


async def _watchdog(attempt_id: str, seconds: float) -> None:
    await asyncio.sleep(seconds)
    spawn(_watchdog_check, attempt_id, label="transfer watchdog check", retries=3)


async def _watchdog_check(attempt_id: str) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if attempt and attempt["status"] in RINGING_STATUSES:
        await _close_attempt(attempt_id, "no_answer", "timeout")


# =============================================================================
# Closer-leg webhook events (routed here by app/calls/lifecycle.py)
# =============================================================================

async def on_closer_answered(attempt_id: str, leg_ccid: str) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if not attempt or attempt["status"] != "ringing":
        await TELNYX.hangup(leg_ccid)
        return
    await repo.update_attempt(attempt_id, {"status": "answered", "leg_control_id": leg_ccid,
                                           "answered_at": _now()})
    runtime = await get_runtime()
    state = _STATES.get(attempt["call_id"])
    if not state or state.done:
        await _close_attempt(attempt_id, "cancelled", "lead_no_longer_waiting")
        return
    if not runtime["closer_require_accept"]:
        await _connect(attempt_id)
        return
    call = await repo.get_call(attempt["call_id"]) or {}
    lead = await repo.get_lead(call["lead_id"]) if call.get("lead_id") else None
    name = (lead or {}).get("first_name") or "a Medicare lead"
    zip_code = spoken_digits(((call.get("qualification") or {}).get("zip_code")) or (lead or {}).get("zip_code") or "")
    coverage = (call.get("qualification") or {}).get("current_coverage")
    whisper = f"Incoming Medicare transfer for {name}"
    if zip_code:
        whisper += f", ZIP code {zip_code}"
    if coverage:
        whisper += f", currently has {coverage}"
    whisper += ". Press 1 to accept the call."
    try:
        await TELNYX.gather_using_speak(leg_ccid, whisper, {"k": "closer", "c": attempt["call_id"], "a": attempt_id})
    except TelnyxError as e:
        logger.warning(f"whisper failed, connecting directly: {e}")
        await _connect(attempt_id)


async def on_closer_gather_ended(attempt_id: str, digits: str | None) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if not attempt or attempt["status"] != "answered":
        return
    if (digits or "").strip().startswith("1"):
        await _connect(attempt_id)
    else:
        await _close_attempt(attempt_id, "declined", f"pressed {digits!r}" if digits else "no key pressed")


async def on_closer_hangup(attempt_id: str, cause: str | None) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if not attempt:
        return
    if attempt["status"] == "connected":
        # The closer finished their conversation with the lead.
        await repo.update_attempt(attempt_id, {"ended_at": _now()})
        if attempt.get("closer_id"):
            await repo.release_closer(attempt["closer_id"], only_if_call_id=attempt["call_id"])
        call = await repo.get_call(attempt["call_id"])
        if call and call.get("status") != "ENDED" and call.get("telnyx_call_control_id"):
            await TELNYX.hangup(call["telnyx_call_control_id"])
        return
    if attempt["status"] in OPEN_ATTEMPT_STATUSES:
        cause_l = (cause or "").lower()
        status = "busy" if "busy" in cause_l else "no_answer" if cause_l in {"timeout", "no_answer", "originator_cancel", ""} else "failed"
        if attempt["status"] == "answered":
            status = "declined"
        elif attempt["status"] == "connecting":
            status = "failed"
        await _close_attempt(attempt_id, status, cause or "hangup", hang_up_leg=False)


async def on_closer_bridged(attempt_id: str) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if attempt and attempt["status"] != "connected":
        await repo.update_attempt(attempt_id, {"status": "connected"})


# =============================================================================
# Lead-leg events
# =============================================================================

async def on_lead_hangup(call_id: str) -> None:
    """The lead's call ended: cancel any closer still ringing and free people up."""
    state = _STATES.pop(call_id, None)
    if state:
        state.done = True
    for attempt in await repo.attempts_for_call(call_id):
        if attempt["status"] in OPEN_ATTEMPT_STATUSES:
            await _close_attempt(attempt["id"], "cancelled", "lead_hung_up", try_next=False)
        elif attempt["status"] == "connected" and not attempt.get("ended_at"):
            await repo.update_attempt(attempt["id"], {"ended_at": _now()})
            if attempt.get("leg_control_id"):
                await TELNYX.hangup(attempt["leg_control_id"])
            if attempt.get("closer_id"):
                await repo.release_closer(attempt["closer_id"], only_if_call_id=call_id)


# =============================================================================
# Internals
# =============================================================================

async def _close_attempt(attempt_id: str, status: str, reason: str, hang_up_leg: bool = True, try_next: bool = True) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if not attempt or attempt["status"] not in OPEN_ATTEMPT_STATUSES:
        return  # already closed by another event — never double-advance
    await repo.update_attempt(attempt_id, {"status": status, "reason": reason, "ended_at": _now()})
    logger.info(f"[transfer {attempt['call_id'][-8:]}] {attempt['closer_name']}: {status} ({reason})")
    if hang_up_leg and attempt.get("leg_control_id"):
        await TELNYX.hangup(attempt["leg_control_id"])
    if attempt.get("closer_id"):
        await repo.release_closer(attempt["closer_id"], only_if_call_id=attempt["call_id"])
    if try_next:
        spawn(_try_next, attempt["call_id"], label=f"transfer {attempt['call_id'][-8:]} next closer", retries=2)


async def _connect(attempt_id: str) -> None:
    attempt = await repo.get_attempt(attempt_id)
    if not attempt or attempt["status"] not in RINGING_STATUSES:
        return
    call_id = attempt["call_id"]
    state = _STATES.get(call_id)
    call = await repo.get_call(call_id)
    if not call or call.get("status") == "ENDED" or not state or state.done:
        await _close_attempt(attempt_id, "cancelled", "lead_no_longer_waiting", try_next=False)
        return
    await repo.update_attempt(attempt_id, {"status": "connecting"})

    session = sessions.get(call_id)
    if session:
        await session.say_and_wait("Great news, I have a licensed agent on the line. I'm connecting you now. Take care!")

    try:
        await TELNYX.bridge(call["telnyx_call_control_id"], attempt["leg_control_id"])
    except TelnyxError as e:
        logger.warning(f"[transfer {call_id[-8:]}] bridge failed: {e}")
        if e.call_gone and (await repo.get_call(call_id) or {}).get("status") == "ENDED":
            await _close_attempt(attempt_id, "cancelled", "lead_hung_up", try_next=False)
        else:
            if session:
                await session.say("Sorry, that didn't go through. Let me try another licensed agent, one moment.")
            await _close_attempt(attempt_id, "failed", f"bridge_error {e.status}")
        return

    state.done = True
    now = _now()
    if session:
        session.transferred = True
    await repo.update_attempt(attempt_id, {"status": "connected"})
    closer = await repo.get_closer(attempt["closer_id"]) if attempt.get("closer_id") else None
    if closer:
        await repo.update_closer(closer["id"], {
            "status": "ON_CALL",
            "current_call_id": call_id,
            "busy_since": now,
            "last_assigned_at": now,
            "total_transfers": int(closer.get("total_transfers") or 0) + 1,
        })
    await repo.update_call(call_id, {
        "status": "TRANSFERRED",
        "ai_state": None,
        "outcome": "transferred",
        "transferred_at": now,
        "closer_id": attempt.get("closer_id"),
        "closer_name": attempt.get("closer_name"),
    })
    if call.get("lead_id"):
        await repo.update_lead(call["lead_id"], {"status": "transferred"})
    # The AI steps out: stop its audio stream and end its pipeline (without hanging up).
    await TELNYX.stop_stream(call["telnyx_call_control_id"])
    if session:
        await session.stop_now()
    logger.info(f"[transfer {call_id[-8:]}] connected to {attempt['closer_name']}")


async def _all_closers_unavailable(call_id: str) -> None:
    """Nobody was free. The AI does NOT book a callback — a licensed agent rings the lead
    back themselves, so the call is flagged for the closers' own call-back list."""
    call = await repo.get_call(call_id)
    if not call or call.get("status") == "ENDED":
        return
    lead = await repo.get_lead(call["lead_id"]) if call.get("lead_id") else None
    await repo.update_call(call_id, {"status": "AI_CONVERSATION", "ai_state": "closing"})
    await outcomes.flag_for_closer_followup(call_id, lead)
    session = sessions.get(call_id)
    if session:
        session.outcome = "no_closer_available"
        await session.end_after_speech(
            "I'm so sorry, all of our licensed agents are helping other people right now. "
            "I'll have one of them call you back as soon as they're free, so they can go over "
            "everything with you. Thank you for your patience, and have a wonderful day."
        )
