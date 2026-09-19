"""Several AI agents sharing one lead list, and the safety brake."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core import runtime as runtime_module
from app.dialer.engine import dialer
from tests.fakes import FakeDB, FakeTelnyx


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    db.install(monkeypatch)
    tel = FakeTelnyx()
    import app.dialer.engine as engine
    monkeypatch.setattr(engine, "TELNYX", tel)
    runtime_module._cache["at"] = 0.0
    dialer._placing.clear()
    dialer.running = False
    dialer.paused_reason = None
    dialer.started_at = None
    return db, tel


def test_the_free_agent_is_picked_in_priority_order(env):
    db, _ = env
    first = db.add_agent("Ava", priority=1, max_concurrent_calls=1)
    second = db.add_agent("Noah", priority=2, max_concurrent_calls=2)

    async def go():
        picked = [(await dialer.pick_agent())["name"]]
        lead = db.add_lead()
        call = db.add_call(lead, agent_id=first["id"], status="AI_CONVERSATION")   # Ava is now at her limit
        picked.append((await dialer.pick_agent())["name"])
        db.add_call(db.add_lead(), agent_id=second["id"], status="AI_CONVERSATION")
        picked.append((await dialer.pick_agent())["name"])
        db.add_call(db.add_lead(), agent_id=second["id"], status="DIALING")        # Noah is full too
        picked.append(await dialer.pick_agent())
        assert call
        return picked

    assert asyncio.run(go()) == ["Ava", "Noah", "Noah", None]


def test_a_switched_off_agent_is_never_used(env):
    db, _ = env
    db.add_agent("Ava", priority=1, enabled=False)
    db.add_agent("Noah", priority=2)
    assert asyncio.run(dialer.pick_agent())["name"] == "Noah"


def test_dialing_records_the_agent_and_its_caller_id(env):
    db, tel = env
    agent = db.add_agent("Noah", from_number="+13055550188")
    lead = db.add_lead(status="calling")

    call = asyncio.run(dialer.place_call(lead, previous_status="new", agent=agent))
    assert call["agent_id"] == agent["id"] and call["agent_name"] == "Noah"
    assert call["lead_zip"] == "33101" and call["lead_state"] == "FL"
    assert tel.dialed[0]["from_number"] == "+13055550188"
    assert db.agents[agent["id"]]["total_calls"] == 1


def test_safety_brake_trips_on_a_run_of_failures(env):
    db, _ = env
    db.config.update({"brake_consecutive_failures": 3, "brake_window_calls": 100})
    runtime_module._cache["at"] = 0.0
    dialer.running = True
    dialer.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i in range(3):
        db.add_call(db.add_lead(), status="ENDED", outcome="no_answer", ended_at=datetime.now(timezone.utc).isoformat())

    asyncio.run(dialer.check_brake())
    assert dialer.running is False
    assert "in a row" in dialer.paused_reason
    assert db.dialer_state["paused_reason"]
    assert any(a["action"] == "dialer_paused_by_safety_brake" for a in db.audits)


def test_safety_brake_trips_on_a_bad_failure_rate(env):
    db, _ = env
    db.config.update({"brake_consecutive_failures": 50, "brake_window_calls": 5, "brake_window_failure_percent": 60})
    runtime_module._cache["at"] = 0.0
    dialer.running = True
    dialer.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    now = datetime.now(timezone.utc)
    for i, outcome in enumerate(["transferred", "no_answer", "busy", "failed", "voicemail"]):
        db.add_call(db.add_lead(), status="ENDED", outcome=outcome,
                    ended_at=(now - timedelta(seconds=i)).isoformat())

    asyncio.run(dialer.check_brake())
    assert dialer.running is False
    assert "%" in dialer.paused_reason


def test_brake_leaves_a_healthy_run_alone(env):
    db, _ = env
    db.config.update({"brake_consecutive_failures": 3, "brake_window_calls": 5, "brake_window_failure_percent": 60})
    runtime_module._cache["at"] = 0.0
    dialer.running = True
    dialer.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    now = datetime.now(timezone.utc)
    for i, outcome in enumerate(["transferred", "no_answer", "transferred", "callback_scheduled", "completed"]):
        db.add_call(db.add_lead(), status="ENDED", outcome=outcome,
                    ended_at=(now - timedelta(seconds=i)).isoformat())

    asyncio.run(dialer.check_brake())
    assert dialer.running is True and dialer.paused_reason is None
