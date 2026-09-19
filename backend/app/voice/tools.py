"""
Function-calling tools the Medicare AI agent can use (Pipecat 1.8.1 direct functions).

Same pattern as the Ashad agent: Pipecat builds each tool's schema from the
function name, type hints and docstring, and the per-call context arrives via
params.app_resources (here: the CallSession for this call).

Tool handlers stay thin — they validate input, update the database, and return
a small result the AI can speak from.
"""

import functools
from datetime import datetime, timedelta, timezone

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame
from pipecat.services.llm_service import FunctionCallParams

from app.calls import outcomes, transfer
from app.calls.session import CallSession
from app.core import hours
from app.db import repo

VALID_END_OUTCOMES = {"not_interested", "not_qualified", "wrong_number", "completed", "callback_scheduled", "do_not_call"}


def _session(params: FunctionCallParams) -> CallSession:
    resources = params.app_resources or {}
    return resources["session"]


def _guarded(fn):
    """If a tool crashes (e.g. the database has a brief outage), the AI still gets an answer right away
    instead of waiting on a tool that never replies, and the error is logged with the call id."""

    @functools.wraps(fn)
    async def wrapper(params: FunctionCallParams, *args, **kwargs):
        original = params.result_callback
        answered = False

        async def result_callback(result, *a, **k):
            nonlocal answered
            answered = True
            return await original(result, *a, **k)

        params.result_callback = result_callback
        try:
            return await fn(params, *args, **kwargs)
        except Exception as e:
            call_id = ((params.app_resources or {}).get("session") or CallSession("?", "", {}, {})).call_id
            logger.exception(f"[call {call_id[-8:]}] tool {fn.__name__} failed: {e}")
            if not answered:
                await original({
                    "success": False,
                    "error": "temporary_system_problem",
                    "instruction": "That didn't go through because of a brief system problem. Don't mention technical "
                                   "details. Try the same tool once more; if it fails again, carry on with the call.",
                })

    return wrapper


@_guarded
async def save_qualification(
    params: FunctionCallParams,
    has_medicare_parts_a_and_b: bool | None = None,
    turning_65_within_3_months: bool | None = None,
    zip_code: str | None = None,
    current_coverage: str | None = None,
    wants_licensed_agent: bool | None = None,
    notes: str | None = None,
):
    """Save the caller's qualification answers. Call it once with ALL the answers collected so far (see the call steps); saving wants_licensed_agent=true for an eligible caller starts the transfer.

    Args:
        has_medicare_parts_a_and_b: True if they have both Medicare Part A and Part B, False if not.
        turning_65_within_3_months: True if they don't have Medicare yet but turn 65 within 3 months.
        zip_code: Their 5-digit ZIP code, digits only.
        current_coverage: Their current coverage in a few words, e.g. Original Medicare, Medicare Advantage, Medicare Supplement, employer coverage, Medicaid, not sure.
        wants_licensed_agent: True if they want to talk with a licensed agent, False if not.
        notes: Anything else useful for the licensed agent, in one short sentence.
    """
    session = _session(params)
    answers = {
        "has_medicare_parts_a_and_b": has_medicare_parts_a_and_b,
        "turning_65_within_3_months": turning_65_within_3_months,
        "zip_code": "".join(ch for ch in (zip_code or "") if ch.isdigit())[:5] or None,
        "current_coverage": (current_coverage or "").strip() or None,
        "wants_licensed_agent": wants_licensed_agent,
        "notes": (notes or "").strip() or None,
    }
    answers = {k: v for k, v in answers.items() if v is not None}
    session.qualification.update(answers)
    try:
        await repo.update_call(session.call_id, {"qualification": session.qualification, "ai_state": "qualifying"})
    except Exception as e:
        logger.warning(f"save_qualification db error: {e}")
    q = session.qualification
    eligible = q.get("has_medicare_parts_a_and_b") is True or q.get("turning_65_within_3_months") is True
    result: dict = {"success": True, "saved": answers}

    if eligible and q.get("wants_licensed_agent") is True and not session.transfer_started:
        # Don't rely on the model remembering to call the transfer tool: start it right here.
        session.transfer_started = True
        result["transfer"] = await transfer.start_transfer(session.call_id)
        result["next_step"] = ("A licensed agent is being connected RIGHT NOW. Tell them warmly to please stay on the "
                               "line for just a moment while you connect them. Do not call transfer_to_licensed_agent "
                               "and do not end the call.")
    elif q.get("has_medicare_parts_a_and_b") is False and "turning_65_within_3_months" not in q:
        result["next_step"] = "Ask whether they turn 65 within the next three months before deciding they don't qualify."
    elif q.get("has_medicare_parts_a_and_b") is False and q.get("turning_65_within_3_months") is False:
        session.outcome = session.outcome or "not_qualified"
        await outcomes.set_outcome(session.call_id, "not_qualified")
        session.arm_auto_end()
        result["next_step"] = ("They don't qualify. Thank them warmly, explain the licensed agents help people who already "
                               "have Medicare, and say goodbye now. The call will end after your goodbye.")
    elif q.get("wants_licensed_agent") is False and not session.transfer_started:
        session.outcome = session.outcome or "not_interested"
        await outcomes.set_outcome(session.call_id, "not_interested")
        session.arm_auto_end()
        result["next_step"] = ("They don't want to talk with an agent. Don't push. Thank them for their time and say "
                               "goodbye now. The call will end after your goodbye.")
    await params.result_callback(result)


@_guarded
async def transfer_to_licensed_agent(params: FunctionCallParams):
    """Connect the caller to a licensed insurance agent. Only use this after they confirmed they have Medicare Part A and B (or turn 65 within 3 months) AND said they want to talk with a licensed agent. Tell them to stay on the line before calling this.
    No arguments.
    """
    session = _session(params)
    q = session.qualification
    eligible = q.get("has_medicare_parts_a_and_b") is True or q.get("turning_65_within_3_months") is True
    if not eligible:
        await params.result_callback({
            "success": False,
            "error": "not_confirmed_eligible",
            "instruction": "First confirm they have Medicare Part A and B (or turn 65 within 3 months) and save it with save_qualification.",
        })
        return
    if q.get("wants_licensed_agent") is False:
        await params.result_callback({"success": False, "error": "caller_declined_agent"})
        return
    session.transfer_started = True
    result = await transfer.start_transfer(session.call_id)
    await params.result_callback({
        "success": True,
        "status": result,
        "instruction": "The transfer is being connected in the background. If the caller speaks, briefly reassure them "
                       "and ask them to stay on the line. Do not end the call and do not call any other tool unless they "
                       "ask to stop being called.",
    })


@_guarded
async def schedule_callback(params: FunctionCallParams, callback_time: str, reason: str = "requested_callback"):
    """Book a callback when now isn't a good time or the person they asked for isn't available.

    Args:
        callback_time: The requested day and time as an ISO 8601 datetime in the caller's local time zone, e.g. 2026-09-18T14:30:00. Use the caller's local date/time from your instructions to work it out. If they give no time, use tomorrow at 11:00.
        reason: Short reason, e.g. bad_timing, not_available, requested_callback.
    """
    session = _session(params)
    lead = session.lead
    runtime = session.runtime
    zone = hours.lead_zone(lead, runtime)
    when: datetime
    try:
        parsed = datetime.fromisoformat(callback_time.strip().replace("Z", "+00:00"))
        when = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    except Exception:
        tomorrow = (datetime.now(timezone.utc).astimezone(zone) + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
        when = tomorrow
    booked = await outcomes.schedule_callback(lead, session.call_id, when.astimezone(timezone.utc), reason or "requested_callback")
    session.outcome = "callback_scheduled"
    session.arm_auto_end()
    spoken = hours.spoken_time(booked, lead, runtime)
    adjusted = abs((booked - when.astimezone(timezone.utc)).total_seconds()) > 600
    await params.result_callback({
        "success": True,
        "booked_for": spoken,
        "adjusted_to_calling_hours": adjusted,
        "instruction": f"Confirm the callback for {spoken}"
                       + (" (explain that's the closest time we're able to call)" if adjusted else "")
                       + ", thank them, say goodbye, then call end_call with outcome callback_scheduled.",
    })


@_guarded
async def mark_do_not_call(params: FunctionCallParams, reason: str = "requested"):
    """Use immediately if the person asks not to be called again, to be removed from the list, or if this is a wrong number.

    Args:
        reason: requested or wrong_number.
    """
    session = _session(params)
    lead = session.lead
    wrong = (reason or "").strip().lower() == "wrong_number"
    try:
        await repo.add_dnc([{
            "phone": lead["phone"],
            "reason": "wrong number" if wrong else "asked not to be called (AI call)",
            "source": "ai_call",
        }])
        await repo.update_leads_by_phone(lead["phone"], {"status": "wrong_number" if wrong else "do_not_call", "next_attempt_at": None})
        for cb in await repo.pending_callbacks_for_lead(lead["id"]):
            await repo.update_callback(cb["id"], {"status": "cancelled", "result": "do_not_call"})
        await outcomes.set_outcome(session.call_id, "wrong_number" if wrong else "do_not_call", overwrite=True)
    except Exception as e:
        logger.error(f"mark_do_not_call failed for call {session.call_id}: {e}")
    session.outcome = "wrong_number" if wrong else "do_not_call"
    session.arm_auto_end()
    await params.result_callback({
        "success": True,
        "instruction": "Say one short polite sentence confirming they won't be called again, then call end_call.",
    })


@_guarded
async def end_call(params: FunctionCallParams, outcome: str = "completed"):
    """Hang up after you have said goodbye.

    Args:
        outcome: One of not_interested, not_qualified, wrong_number, callback_scheduled, do_not_call, completed.
    """
    session = _session(params)
    if transfer.is_transferring(session.call_id):
        await params.result_callback({
            "success": False,
            "error": "transfer_in_progress",
            "instruction": "A licensed agent is being connected. Keep the caller on the line.",
        })
        return
    outcome = outcome if outcome in VALID_END_OUTCOMES else "completed"
    final = session.outcome or outcome
    await outcomes.set_outcome(session.call_id, final)
    session.outcome = final
    session.ending = True
    await params.result_callback({"success": True})
    # Pushed downstream so the goodbye that's already queued is spoken before the pipeline ends.
    await params.llm.push_frame(EndWorkerFrame())


MEDICARE_TOOLS = [
    save_qualification,
    transfer_to_licensed_agent,
    schedule_callback,
    mark_do_not_call,
    end_call,
]
