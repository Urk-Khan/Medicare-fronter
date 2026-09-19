import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.calls import lifecycle, transfer
from app.calls.session import CallSession
from app.core import runtime as runtime_module
from app.core.runtime import DEFAULTS
from app.voice import tools
from tests.fakes import FakeDB, FakeTelnyx


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


def make_params(session):
    results = []

    async def result_callback(value):
        results.append(value)

    llm = SimpleNamespace(push_frame=AsyncMock())
    return SimpleNamespace(app_resources={"session": session}, result_callback=result_callback, llm=llm), results


async def test_qualified_and_willing_starts_transfer_automatically(env):
    db, tel = env
    db.add_closer("Agent 1", "+14158670001")
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, results = make_params(session)

    await tools.save_qualification(params, has_medicare_parts_a_and_b=True, zip_code="33101")
    assert "transfer" not in results[-1]
    await tools.save_qualification(params, wants_licensed_agent=True)
    await asyncio.sleep(0.05)
    assert results[-1]["transfer"] == "connecting"
    assert session.transfer_started
    assert tel.dialed and tel.dialed[0]["to"] == "+14158670001"
    assert db.calls[call["id"]]["qualification"]["zip_code"] == "33101"

    # A second explicit tool call must not start a second transfer.
    await tools.transfer_to_licensed_agent(params)
    await asyncio.sleep(0.05)
    assert len(tel.dialed) == 1


async def test_transfer_refused_without_eligibility(env):
    db, tel = env
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, results = make_params(session)
    await tools.transfer_to_licensed_agent(params)
    assert results[-1]["success"] is False
    assert tel.dialed == []


async def test_not_qualified_arms_auto_hangup(env):
    db, _ = env
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, results = make_params(session)
    await tools.save_qualification(params, has_medicare_parts_a_and_b=False)
    assert "turn 65" in results[-1]["next_step"]
    assert not session.auto_end_armed
    await tools.save_qualification(params, turning_65_within_3_months=False)
    assert session.auto_end_armed
    assert db.calls[call["id"]]["outcome"] == "not_qualified"


async def test_mark_do_not_call(env):
    db, _ = env
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, _ = make_params(session)
    await tools.mark_do_not_call(params, reason="requested")
    assert lead["phone"] in db.dnc
    assert db.leads[lead["id"]]["status"] == "do_not_call"
    assert db.calls[call["id"]]["outcome"] == "do_not_call"
    assert session.auto_end_armed


async def test_end_call_blocked_during_transfer(env):
    db, _ = env
    db.add_closer("Agent 1", "+14158670001")
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, results = make_params(session)
    await transfer.start_transfer(call["id"])
    await asyncio.sleep(0.05)
    await tools.end_call(params, outcome="not_interested")
    assert results[-1]["error"] == "transfer_in_progress"
    params.llm.push_frame.assert_not_awaited()


async def test_tool_crash_still_answers_the_ai(env, monkeypatch):
    db, tel = env
    lead = db.add_lead()
    call = db.add_call(lead)
    session = CallSession(call_id=call["id"], call_control_id="x", lead=lead, runtime=dict(DEFAULTS))
    params, results = make_params(session)

    async def boom(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tools.outcomes, "schedule_callback", boom)
    await tools.schedule_callback(params, callback_time="2030-01-02T14:00:00")
    assert len(results) == 1
    assert results[0]["success"] is False and results[0]["error"] == "temporary_system_problem"


def test_api_requires_login_and_login_works():
    import app.main as main
    from app.auth.security import hash_password

    user = {"id": "u1", "username": "admin", "password_hash": hash_password("correct horse"), "role": "admin"}
    with patch("app.main.seed_defaults", AsyncMock()), patch("app.main.dialer.boot", lambda: None), \
         patch("app.api.public.repo.get_user_by_username", AsyncMock(return_value=user)), \
         patch("app.api.public.repo.update_user", AsyncMock()), patch("app.api.public.repo.audit", AsyncMock()), \
         patch("app.auth.security.repo.get_user_by_id", AsyncMock(return_value=user)):
        with TestClient(main.app) as client:
            assert client.get("/api/leads").status_code == 401
            assert client.post("/api/calling/start").status_code == 401
            assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
            token = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse"}).json()["access_token"]
            me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert me.status_code == 200 and me.json()["username"] == "admin"


def test_webhook_rejects_bad_signature(monkeypatch):
    import app.main as main
    from app.config import settings

    monkeypatch.setattr(settings, "telnyx_webhook_public_key", "MCowBQYDK2VwAyEA" + "A" * 28)
    with patch("app.main.seed_defaults", AsyncMock()), patch("app.main.dialer.boot", lambda: None):
        with TestClient(main.app) as client:
            resp = client.post("/webhooks/telnyx", content=b'{"data":{}}',
                               headers={"telnyx-signature-ed25519": "bad", "telnyx-timestamp": "1"})
            assert resp.status_code == 401
