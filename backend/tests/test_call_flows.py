"""End-to-end call logic with an in-memory database and a fake Telnyx."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.calls import lifecycle, outcomes, transfer
from app.core import runtime as runtime_module
from app.telephony.telnyx import encode_state
from tests.fakes import FakeDB, FakeTelnyx


async def settle(rounds: int = 30):
    for _ in range(rounds):
        await asyncio.sleep(0.005)


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    db.install(monkeypatch)
    tel = FakeTelnyx()
    for module in (transfer, lifecycle):
        monkeypatch.setattr(module, "TELNYX", tel)

    async def no_watchdog(attempt_id, seconds):
        return None

    monkeypatch.setattr(transfer, "_watchdog", no_watchdog)
    transfer._STATES.clear()
    runtime_module._cache["at"] = 0.0
    return db, tel


def closer_event(event_type, attempt, leg, **payload):
    return {"id": f"{event_type}-{attempt['id']}-{len(payload)}-{leg}", "event_type": event_type,
            "payload": {"call_control_id": leg, "client_state": encode_state({"k": "closer", "c": attempt["call_id"], "a": attempt["id"]}), **payload}}


async def test_transfer_skips_busy_closer_and_connects_next(env):
    db, tel = env
    busy = db.add_closer("Busy Bob", "+14158670001", priority=1, status="ON_CALL")
    away = db.add_closer("Away Ann", "+14158670002", priority=1, availability="away")
    free = db.add_closer("Free Fran", "+14158670003", priority=2)
    lead = db.add_lead()
    call = db.add_call(lead)

    assert await transfer.start_transfer(call["id"]) == "connecting"
    await settle()
    assert [d["to"] for d in tel.dialed] == ["+14158670003"], "busy and away closers must be skipped"
    attempt = next(iter(db.attempts.values()))
    assert db.closers[free["id"]]["status"] == "RINGING"

    await lifecycle.handle_event(closer_event("call.answered", attempt, "leg-1"))
    assert tel.gathers and "Press 1" in tel.gathers[0]
    await lifecycle.handle_event(closer_event("call.gather.ended", attempt, "leg-1", digits="1"))
    await settle()

    assert tel.bridged == [(call["telnyx_call_control_id"], "leg-1")]
    assert db.calls[call["id"]]["status"] == "TRANSFERRED"
    assert db.calls[call["id"]]["outcome"] == "transferred"
    assert db.closers[free["id"]]["status"] == "ON_CALL"
    assert db.leads[lead["id"]]["status"] == "transferred"
    assert db.closers[busy["id"]]["status"] == "ON_CALL" and db.closers[away["id"]]["status"] == "FREE"


async def test_no_answer_rolls_over_to_next_closer(env):
    db, tel = env
    first = db.add_closer("Agent 1", "+14158670001", priority=1)
    second = db.add_closer("Agent 2", "+14158670002", priority=2)
    lead = db.add_lead()
    call = db.add_call(lead)

    await transfer.start_transfer(call["id"])
    await settle()
    attempt1 = next(iter(db.attempts.values()))
    await lifecycle.handle_event(closer_event("call.hangup", attempt1, "leg-1", hangup_cause="timeout"))
    await settle()

    assert db.attempts[attempt1["id"]]["status"] == "no_answer"
    assert db.closers[first["id"]]["status"] == "FREE"
    assert [d["to"] for d in tel.dialed] == ["+14158670001", "+14158670002"]
    assert db.closers[second["id"]]["status"] == "RINGING"


async def test_declined_whisper_and_dial_error_roll_over(env):
    db, tel = env
    db.add_closer("Agent 1", "+14158670001", priority=1)
    db.add_closer("Agent 2", "+14158670002", priority=2)
    db.add_closer("Agent 3", "+14158670003", priority=3)
    tel.fail_dial_to.add("+14158670002")
    lead = db.add_lead()
    call = db.add_call(lead)

    await transfer.start_transfer(call["id"])
    await settle()
    attempt1 = next(a for a in db.attempts.values() if a["destination"] == "+14158670001")
    await lifecycle.handle_event(closer_event("call.answered", attempt1, "leg-1"))
    await lifecycle.handle_event(closer_event("call.gather.ended", attempt1, "leg-1", digits="2"))
    await settle()

    statuses = {a["destination"]: a["status"] for a in db.attempts.values()}
    assert statuses["+14158670001"] == "declined"
    assert statuses["+14158670002"] == "failed"
    assert statuses["+14158670003"] == "ringing"
    assert "leg-1" in tel.hung_up


async def test_all_closers_unavailable_flags_lead_for_the_closers(env):
    """Nobody free: the AI does not book a callback — the lead goes on the closers' own list."""
    db, tel = env
    db.add_closer("Agent 1", "+14158670001", status="ON_CALL")
    db.add_closer("No number", "")
    lead = db.add_lead()
    call = db.add_call(lead)

    await transfer.start_transfer(call["id"])
    await settle()
    assert tel.dialed == []
    assert db.callbacks == {}
    assert db.calls[call["id"]]["outcome"] == "no_closer_available"
    assert db.calls[call["id"]]["needs_closer_followup"] is True
    assert db.calls[call["id"]]["followup_status"] == "pending"
    assert db.leads[lead["id"]]["status"] == "awaiting_closer"
    assert db.leads[lead["id"]]["next_attempt_at"] is None


async def test_lead_hangup_cancels_ringing_closer(env):
    db, tel = env
    closer = db.add_closer("Agent 1", "+14158670001")
    lead = db.add_lead()
    call = db.add_call(lead, status="TRANSFERRING")
    await transfer.start_transfer(call["id"])
    await settle()

    await lifecycle.handle_event({"id": "hang-1", "event_type": "call.hangup",
                                  "payload": {"call_control_id": call["telnyx_call_control_id"], "hangup_cause": "normal_clearing",
                                              "client_state": encode_state({"k": "lead", "c": call["id"]})}})
    await settle()
    attempt = next(iter(db.attempts.values()))
    assert attempt["status"] == "cancelled"
    assert "leg-1" in tel.hung_up
    assert db.closers[closer["id"]]["status"] == "FREE"
    assert db.calls[call["id"]]["status"] == "ENDED"


async def test_voicemail_is_hung_up_and_retried(env):
    db, tel = env
    lead = db.add_lead()
    call = db.add_call(lead, status="RINGING", answered_at=None)
    state = encode_state({"k": "lead", "c": call["id"]})
    ccid = call["telnyx_call_control_id"]
    await lifecycle.handle_event({"id": "a1", "event_type": "call.answered", "occurred_at": datetime.now(timezone.utc).isoformat(),
                                  "payload": {"call_control_id": ccid, "client_state": state}})
    await lifecycle.handle_event({"id": "m1", "event_type": "call.machine.detection.ended",
                                  "payload": {"call_control_id": ccid, "client_state": state, "result": "machine"}})
    assert ccid in tel.hung_up and tel.streams == []
    await lifecycle.handle_event({"id": "h1", "event_type": "call.hangup",
                                  "payload": {"call_control_id": ccid, "client_state": state, "hangup_cause": "normal_clearing"}})
    assert db.calls[call["id"]]["outcome"] == "voicemail"
    assert db.leads[lead["id"]]["status"] == "retry_scheduled"
    assert db.leads[lead["id"]]["retry_count"] == 1
    assert db.leads[lead["id"]]["current_call_id"] is None


async def test_human_answer_starts_ai_stream_once(env):
    db, tel = env
    lead = db.add_lead()
    call = db.add_call(lead, status="RINGING", answered_at=None)
    state = encode_state({"k": "lead", "c": call["id"]})
    ccid = call["telnyx_call_control_id"]
    await lifecycle.handle_event({"id": "a1", "event_type": "call.answered", "payload": {"call_control_id": ccid, "client_state": state}})
    await lifecycle.handle_event({"id": "m1", "event_type": "call.machine.detection.ended",
                                  "payload": {"call_control_id": ccid, "client_state": state, "result": "human"}})
    await lifecycle.start_ai(call["id"])  # e.g. the fallback timer firing too
    assert len(tel.streams) == 1
    assert "token=" in tel.streams[0] and tel.streams[0].startswith("wss://")
    assert db.calls[call["id"]]["status"] == "AI_CONVERSATION"


async def test_duplicate_webhooks_are_ignored(env):
    db, tel = env
    lead = db.add_lead()
    call = db.add_call(lead, status="RINGING", answered_at=None)
    event = {"id": "dup", "event_type": "call.hangup",
             "payload": {"call_control_id": call["telnyx_call_control_id"], "hangup_cause": "timeout",
                         "client_state": encode_state({"k": "lead", "c": call["id"]})}}
    await lifecycle.handle_event(event)
    await lifecycle.handle_event(event)
    assert db.leads[lead["id"]]["retry_count"] == 1


async def test_retries_stop_after_the_limit_for_that_reason(env):
    db, _ = env
    # Default rule: try again three times after a no-answer.
    lead = db.add_lead(retry_count=3, attempt_counts={"no_answer": 3})
    call = db.add_call(lead, answered_at=None, status="RINGING")
    await outcomes.apply_lead_disposition(call, "no_answer")
    assert db.leads[lead["id"]]["status"] == "no_answer_final"
    assert db.leads[lead["id"]]["attempt_counts"]["no_answer"] == 4


async def test_each_reason_has_its_own_wait_and_counter(env):
    db, _ = env
    lead = db.add_lead()
    # Busy waits 1 hour by default; a hang-up waits 48 hours.
    busy_call = db.add_call(lead, answered_at=None, status="RINGING")
    await outcomes.apply_lead_disposition(busy_call, "busy")
    busy_at = datetime.fromisoformat(db.leads[lead["id"]]["next_attempt_at"])
    assert db.leads[lead["id"]]["attempt_counts"] == {"busy": 1}
    assert db.leads[lead["id"]]["status"] == "retry_scheduled"

    db.leads[lead["id"]]["current_call_id"] = None
    hangup_call = db.add_call(db.leads[lead["id"]])
    await outcomes.apply_lead_disposition(hangup_call, "caller_hangup")
    hangup_at = datetime.fromisoformat(db.leads[lead["id"]]["next_attempt_at"])
    assert db.leads[lead["id"]]["attempt_counts"] == {"busy": 1, "hangup": 1}
    assert hangup_at > busy_at + timedelta(hours=24)


async def test_retry_time_of_day_pushes_the_next_attempt_later(env):
    db, _ = env
    lead = db.add_lead(timezone="America/New_York")
    call = db.add_call(lead, answered_at=None, status="RINGING")
    db.config.update({"retry_time_of_day": "17:00", "retry_no_answer_hours": 1,
                      "calling_days": [0, 1, 2, 3, 4, 5, 6]})
    runtime_module._cache["at"] = 0.0
    await outcomes.apply_lead_disposition(call, "no_answer")
    when = datetime.fromisoformat(db.leads[lead["id"]]["next_attempt_at"])
    local = when.astimezone(ZoneInfo("America/New_York"))
    assert local.strftime("%H:%M") >= "17:00", local


async def test_callback_outcome_keeps_schedule(env):
    db, _ = env
    lead = db.add_lead()
    call = db.add_call(lead)
    when = await outcomes.schedule_callback(lead, call["id"], datetime(2030, 1, 8, 20, 0, tzinfo=timezone.utc), "bad_timing")
    await outcomes.apply_lead_disposition(db.calls[call["id"]], "callback_scheduled")
    assert db.leads[lead["id"]]["status"] == "callback_scheduled"
    assert db.leads[lead["id"]]["next_attempt_at"] == when.isoformat()


async def test_dnc_outcome_is_final(env):
    db, _ = env
    lead = db.add_lead(status="calling")
    call = db.add_call(lead)
    await outcomes.apply_lead_disposition(call, "do_not_call")
    assert db.leads[lead["id"]]["status"] == "do_not_call"
    assert db.leads[lead["id"]]["next_attempt_at"] is None
