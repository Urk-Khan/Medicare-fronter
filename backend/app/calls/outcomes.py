"""
What happens to a lead after a call ends: final status, retries, callbacks.

This is the single place that decides "call again later?" so the dialer, the
webhooks and the scheduler all behave the same way.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from app.core import hours
from app.core.runtime import RETRY_RULES, get_runtime
from app.db import repo

# Outcome -> lead status when the outcome is final (no more automatic calls).
FINAL_LEAD_STATUS = {
    "transferred": "transferred",
    "no_closer_available": "awaiting_closer",
    "do_not_call": "do_not_call",
    "wrong_number": "wrong_number",
    "not_interested": "not_interested",
    "not_qualified": "not_qualified",
    "bad_number": "failed",
    "completed": "contacted",
}

# Outcomes where we couldn't have a conversation — try again later.
RETRY_OUTCOMES = {"no_answer", "busy", "voicemail", "caller_hangup", "caller_hangup_early", "failed", "no_conversation"}

# Which retry rule (wait + max tries on the Settings page) applies to each of those outcomes.
RETRY_GROUP = {
    "no_answer": "no_answer",
    "failed": "no_answer",
    "no_conversation": "no_answer",
    "busy": "busy",
    "voicemail": "voicemail",
    "caller_hangup": "hangup",
    "caller_hangup_early": "hangup",
}

ALL_OUTCOMES = sorted(set(FINAL_LEAD_STATUS) | RETRY_OUTCOMES | {"callback_scheduled"})

# Outcomes that mean "this call did not reach a person" — the safety brake watches these.
FAILURE_OUTCOMES = {"no_answer", "busy", "failed", "bad_number", "voicemail", "no_conversation"}

_BAD_NUMBER_CAUSES = {"unallocated_number", "invalid_number_format", "number_changed", "incompatible_destination"}
_BUSY_CAUSES = {"user_busy", "busy"}
_NO_ANSWER_CAUSES = {"timeout", "no_answer", "originator_cancel", "call_rejected", "normal_temporary_failure"}


def outcome_from_hangup(call: dict, cause: str | None) -> str:
    """Decide an outcome for a call that ended without the AI setting one."""
    cause = (cause or "").lower()
    if call.get("outcome"):
        return call["outcome"]
    if call.get("amd_result") == "machine":
        return "voicemail"
    if cause in _BAD_NUMBER_CAUSES:
        return "bad_number"
    if not call.get("answered_at"):
        if cause in _BUSY_CAUSES:
            return "busy"
        return "no_answer"
    if call.get("status") in ("AI_CONVERSATION", "TRANSFERRING"):
        return "caller_hangup"
    return "caller_hangup_early"


async def apply_lead_disposition(call: dict, outcome: str) -> None:
    lead_id = call.get("lead_id")
    if not lead_id:
        return
    lead = await repo.get_lead(lead_id)
    if not lead:
        return
    runtime = await get_runtime()
    now = datetime.now(timezone.utc)
    update: dict = {
        "current_call_id": None,
        "last_call_at": call.get("started_at") or now.isoformat(),
        "last_outcome": outcome,
    }
    if call.get("qualification"):
        update["qualification"] = {**(lead.get("qualification") or {}), **call["qualification"]}
        zip_code = call["qualification"].get("zip_code")
        if zip_code:
            update["zip_code"] = zip_code

    callback_rows = await repo.pending_callbacks_for_lead(lead_id) if call.get("is_callback") else []

    if outcome == "callback_scheduled":
        # The tool/transfer engine already set status + next_attempt_at; keep them.
        if lead.get("status") != "callback_scheduled":
            update["status"] = "callback_scheduled"
    elif outcome in FINAL_LEAD_STATUS:
        update["status"] = FINAL_LEAD_STATUS[outcome]
        update["next_attempt_at"] = None
        for cb in callback_rows:
            await repo.update_callback(cb["id"], {"status": "completed", "result": outcome, "completed_at": now.isoformat()})
    else:
        # Retry rules are per reason: "nobody picked up" and "they hung up on us" get
        # different waits and different numbers of tries.
        group = RETRY_GROUP.get(outcome, "no_answer")
        wait_key, max_key = RETRY_RULES[group]
        counts = dict(lead.get("attempt_counts") or {})
        counts[group] = int(counts.get(group) or 0) + 1
        retries = int(lead.get("retry_count") or 0) + 1
        update["retry_count"] = retries
        update["attempt_counts"] = counts
        # The settings say how many times to TRY AGAIN, so the first call doesn't count.
        group_done = counts[group] > int(runtime[max_key])
        overall_done = retries > int(runtime["max_retries"])
        if group_done or overall_done:
            update["status"] = "no_answer_final"
            update["next_attempt_at"] = None
            for cb in callback_rows:
                await repo.update_callback(cb["id"], {"status": "completed", "result": "unreachable", "completed_at": now.isoformat()})
        else:
            earliest = now + timedelta(hours=float(runtime[wait_key]))
            next_at = hours.next_retry_time(lead, runtime, earliest)
            update["next_attempt_at"] = next_at.isoformat()
            if callback_rows:
                # Keep it as a callback (dialed with priority) and move the callback time.
                update["status"] = "callback_scheduled"
                for cb in callback_rows:
                    await repo.update_callback(cb["id"], {"status": "pending", "scheduled_for": next_at.isoformat(), "result": outcome})
            else:
                update["status"] = "retry_scheduled"
    await repo.update_lead(lead_id, update)
    logger.info(f"lead {lead_id}: outcome={outcome} -> status={update.get('status', lead.get('status'))}")


async def schedule_callback(lead: dict, call_id: str | None, when_utc: datetime, reason: str) -> datetime:
    """Book a callback inside the lead's calling window. Returns the (possibly adjusted) time."""
    runtime = await get_runtime()
    now = datetime.now(timezone.utc)
    when_utc = max(when_utc, now + timedelta(minutes=5))
    when_utc = hours.next_allowed_time(lead, runtime, when_utc)
    for cb in await repo.pending_callbacks_for_lead(lead["id"]):
        if call_id is None or cb.get("call_id") != call_id:
            await repo.update_callback(cb["id"], {"status": "cancelled", "result": "replaced_by_new_callback"})
    await repo.insert_callback({
        "lead_id": lead["id"],
        "call_id": call_id,
        "lead_name": f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
        "phone": lead["phone"],
        "reason": reason,
        "scheduled_for": when_utc.isoformat(),
        "status": "pending",
    })
    await repo.update_lead(lead["id"], {"status": "callback_scheduled", "next_attempt_at": when_utc.isoformat()})
    if call_id:
        await set_outcome(call_id, "callback_scheduled")
    return when_utc


async def flag_for_closer_followup(call_id: str, lead: dict | None = None) -> None:
    """Every closer was busy: mark the call for the closers' own call-back list.
    The dialer never calls this lead again — a licensed agent rings them back."""
    await repo.update_call(call_id, {
        "needs_closer_followup": True,
        "followup_status": "pending",
        "outcome": "no_closer_available",
    })
    if lead:
        for cb in await repo.pending_callbacks_for_lead(lead["id"]):
            await repo.update_callback(cb["id"], {"status": "cancelled", "result": "closer_will_call_back"})
        await repo.update_lead(lead["id"], {"status": "awaiting_closer", "next_attempt_at": None})
    logger.info(f"call {call_id[-8:]}: no closer was free — flagged for the closers' call-back list")


async def set_outcome(call_id: str, outcome: str, overwrite: bool = False) -> None:
    call = await repo.get_call(call_id)
    if not call:
        return
    if call.get("outcome") and not overwrite:
        return
    await repo.update_call(call_id, {"outcome": outcome})
