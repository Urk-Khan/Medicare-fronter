"""Dashboard REST API. Every route here requires a logged-in user.

Two roles:
  super_admin — everything, including the AI agents' scripts, the dialer/retry/transfer
                settings, the audit log and the user accounts.
  admin       — everything else (calls, leads, closers, do-not-call, imports, basic settings).
Role checks live here, on the server, so hiding a page in the dashboard is never the only
thing protecting it.
"""

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from app.auth.security import (ADMIN, SUPER_ADMIN, current_user, hash_password, is_super_admin,
                               require_super_admin, verify_password)
from app.calls import lifecycle, outcomes
from app.config import settings
from app.core import hours
from app.core.phones import normalize_phone, pretty_phone, timezone_for_phone
from app.core.runtime import DEFAULTS, SUPER_ADMIN_KEYS, get_runtime, save_runtime
from app.db import repo
from app.dialer.engine import DialError, dialer
from app.imports import leads as lead_import
from app.imports import sheets
from app.telephony.telnyx import TELNYX
from app.voice import script as script_mod

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    return (page - 1) * page_size, page_size


async def _range_start(period: str) -> str:
    """'today' or 'month' turned into a UTC timestamp, using the company's own time zone
    so "today" means today where the business is, not in UTC."""
    runtime = await get_runtime()
    try:
        zone = ZoneInfo(runtime.get("default_timezone") or "America/New_York")
    except Exception:
        zone = ZoneInfo("America/New_York")
    local = _now().astimezone(zone)
    if period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


# =============================================================================
# Account
# =============================================================================

@router.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "full_name": user.get("full_name") or "",
            "role": user.get("role") or ADMIN, "is_super_admin": is_super_admin(user)}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/auth/change-password")
async def change_password(body: PasswordChange, user: dict = Depends(current_user)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    await repo.update_user(user["id"], {"password_hash": hash_password(body.new_password)})
    await repo.audit(user["username"], "password_changed")
    return {"success": True}


# =============================================================================
# Users (super admin only)
# =============================================================================

@router.get("/users")
async def users_list(_: dict = Depends(require_super_admin)):
    return {"items": await repo.list_users()}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=200)
    full_name: str = ""
    role: str = ADMIN


@router.post("/users")
async def user_create(body: UserIn, user: dict = Depends(require_super_admin)):
    username = body.username.strip().lower()
    if not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        raise HTTPException(400, "Usernames can only contain letters, numbers, dots, dashes and underscores.")
    if body.role not in (SUPER_ADMIN, ADMIN):
        raise HTTPException(400, "Role must be admin or super_admin.")
    if await repo.get_user_by_username(username):
        raise HTTPException(409, "That username is already taken.")
    created = await repo.insert_user({
        "username": username,
        "password_hash": hash_password(body.password),
        "full_name": body.full_name.strip(),
        "role": body.role,
        "enabled": True,
        "created_by": user["username"],
    })
    await repo.audit(user["username"], "user_created", {"username": username, "role": body.role})
    created.pop("password_hash", None)
    return created


class UserPatch(BaseModel):
    full_name: str | None = None
    role: str | None = None
    enabled: bool | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=200)


@router.patch("/users/{user_id}")
async def user_update(user_id: str, body: UserPatch, user: dict = Depends(require_super_admin)):
    target = await repo.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    data: dict[str, Any] = {}
    if body.full_name is not None:
        data["full_name"] = body.full_name.strip()
    if body.role is not None:
        if body.role not in (SUPER_ADMIN, ADMIN):
            raise HTTPException(400, "Role must be admin or super_admin.")
        if target["role"] == SUPER_ADMIN and body.role != SUPER_ADMIN and await repo.count_users_with_role(SUPER_ADMIN) <= 1:
            raise HTTPException(400, "There must always be at least one super admin.")
        data["role"] = body.role
    if body.enabled is not None:
        if not body.enabled and target["id"] == user["id"]:
            raise HTTPException(400, "You can't disable your own account.")
        data["enabled"] = body.enabled
    if body.new_password:
        data["password_hash"] = hash_password(body.new_password)
    if not data:
        raise HTTPException(400, "Nothing to update.")
    await repo.update_user(user_id, data)
    await repo.audit(user["username"], "user_updated",
                     {"username": target["username"], "fields": sorted(k for k in data if k != "password_hash"),
                      "password_reset": bool(body.new_password)})
    return {"success": True}


@router.delete("/users/{user_id}")
async def user_delete(user_id: str, user: dict = Depends(require_super_admin)):
    target = await repo.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target["id"] == user["id"]:
        raise HTTPException(400, "You can't delete your own account.")
    if target["role"] == SUPER_ADMIN and await repo.count_users_with_role(SUPER_ADMIN) <= 1:
        raise HTTPException(400, "There must always be at least one super admin.")
    await repo.delete_user(user_id)
    await repo.audit(user["username"], "user_deleted", {"username": target["username"]})
    return {"success": True}


# =============================================================================
# AI agents (super admin only — each agent owns its script, voice and number)
# =============================================================================

def _agent_view(agent: dict, live: int = 0) -> dict:
    return {**agent, "live_calls": live,
            "from_number_pretty": pretty_phone(agent.get("from_number") or ""),
            "script_set": bool((agent.get("system_prompt") or "").strip())}


@router.get("/agents")
async def agents_list(user: dict = Depends(current_user)):
    agents = await repo.list_agents()
    active = await repo.active_calls()
    items = []
    for agent in agents:
        live = len([c for c in active if c.get("agent_id") == agent["id"]])
        view = _agent_view(agent, live)
        if not is_super_admin(user):
            # An ordinary admin may see which agents exist (for filters), not their scripts.
            for key in ("system_prompt", "opening_line", "disclaimer_text"):
                view.pop(key, None)
        items.append(view)
    return {"items": items}


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    label: str = ""
    enabled: bool = True
    voice_id: str = ""
    from_number: str = ""
    max_concurrent_calls: int = 3
    priority: int = 1
    opening_line: str = ""
    system_prompt: str = ""
    disclaimer_text: str = ""


def _check_script(opening_line: str, system_prompt: str, disclaimer_text: str) -> None:
    unknown = sorted(set(script_mod.unknown_placeholders(opening_line))
                     | set(script_mod.unknown_placeholders(system_prompt))
                     | set(script_mod.unknown_placeholders(disclaimer_text)))
    if unknown:
        raise HTTPException(400, f"Unknown placeholders: {', '.join('{{' + u + '}}' for u in unknown)}")


def _clean_from_number(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    phone = normalize_phone(value)
    if not phone:
        raise HTTPException(400, "The agent's phone number must be a valid number, or left empty to use the main one.")
    return phone


@router.post("/agents")
async def agent_create(body: AgentIn, user: dict = Depends(require_super_admin)):
    _check_script(body.opening_line, body.system_prompt, body.disclaimer_text)
    agent = await repo.insert_agent({
        "name": body.name.strip(),
        "label": body.label.strip(),
        "enabled": body.enabled,
        "voice_id": body.voice_id.strip(),
        "from_number": _clean_from_number(body.from_number),
        "max_concurrent_calls": max(1, min(20, body.max_concurrent_calls)),
        "priority": max(1, body.priority),
        "opening_line": body.opening_line.strip() or script_mod.DEFAULT_OPENING_LINE,
        "system_prompt": body.system_prompt.strip() or script_mod.DEFAULT_SYSTEM_PROMPT,
        "disclaimer_text": body.disclaimer_text.strip() or script_mod.DEFAULT_DISCLAIMER,
    })
    await repo.audit(user["username"], "agent_created", {"agent_id": agent.get("id"), "name": body.name})
    return agent


class AgentPatch(BaseModel):
    name: str | None = None
    label: str | None = None
    enabled: bool | None = None
    voice_id: str | None = None
    from_number: str | None = None
    max_concurrent_calls: int | None = None
    priority: int | None = None
    opening_line: str | None = Field(default=None, min_length=10, max_length=600)
    system_prompt: str | None = Field(default=None, min_length=200, max_length=30000)
    disclaimer_text: str | None = Field(default=None, min_length=20, max_length=1500)


@router.patch("/agents/{agent_id}")
async def agent_update(agent_id: str, body: AgentPatch, user: dict = Depends(require_super_admin)):
    agent = await repo.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if {"opening_line", "system_prompt", "disclaimer_text"} & set(data):
        _check_script(data.get("opening_line") or agent.get("opening_line") or "",
                      data.get("system_prompt") or agent.get("system_prompt") or "",
                      data.get("disclaimer_text") or agent.get("disclaimer_text") or "")
    if "from_number" in data:
        data["from_number"] = _clean_from_number(data["from_number"])
    if "max_concurrent_calls" in data:
        data["max_concurrent_calls"] = max(1, min(20, int(data["max_concurrent_calls"])))
    if "priority" in data:
        data["priority"] = max(1, int(data["priority"]))
    if "name" in data:
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "The agent needs a name — it's what the AI calls itself on the phone.")
    await repo.update_agent(agent_id, data)
    await repo.audit(user["username"], "agent_updated",
                     {"agent_id": agent_id, "name": agent.get("name"), "fields": sorted(data)})
    return {"success": True}


@router.post("/agents/{agent_id}/reset-script")
async def agent_reset_script(agent_id: str, user: dict = Depends(require_super_admin)):
    if not await repo.get_agent(agent_id):
        raise HTTPException(404, "Agent not found")
    await repo.update_agent(agent_id, {
        "opening_line": script_mod.DEFAULT_OPENING_LINE,
        "system_prompt": script_mod.DEFAULT_SYSTEM_PROMPT,
        "disclaimer_text": script_mod.DEFAULT_DISCLAIMER,
    })
    await repo.audit(user["username"], "agent_script_reset", {"agent_id": agent_id})
    return {"success": True}


@router.delete("/agents/{agent_id}")
async def agent_delete(agent_id: str, user: dict = Depends(require_super_admin)):
    agent = await repo.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if await repo.count_active_calls_for_agent(agent_id):
        raise HTTPException(400, "This agent is on a call right now.")
    if await repo.count_agents() <= 1:
        raise HTTPException(400, "Keep at least one AI agent — the dialer needs one to make calls.")
    await repo.delete_agent(agent_id)
    await repo.audit(user["username"], "agent_deleted", {"agent_id": agent_id, "name": agent["name"]})
    return {"success": True}


class ScriptPreviewIn(BaseModel):
    name: str = "Ava"
    opening_line: str
    system_prompt: str
    disclaimer_text: str


@router.post("/agents/preview")
async def agent_script_preview(body: ScriptPreviewIn, _: dict = Depends(require_super_admin)):
    runtime = await get_runtime()
    sample = {"first_name": "Mary", "last_name": "Johnson", "phone": "+13055550123", "state": "FL",
              "zip_code": "33101", "source": "requested Medicare information on a website form",
              "notes": "Prefers afternoon calls", "timezone": "America/New_York"}
    values = script_mod.build_values(sample, runtime, body.disclaimer_text, agent_name=body.name)
    return {
        "opening_line": script_mod.render(body.opening_line, values),
        "system_prompt": script_mod.render(body.system_prompt, values),
        "disclaimer": values["disclaimer"],
        "placeholders": script_mod.PLACEHOLDERS,
    }


@router.get("/script/defaults")
async def script_defaults(_: dict = Depends(require_super_admin)):
    return {
        "opening_line": script_mod.DEFAULT_OPENING_LINE,
        "system_prompt": script_mod.DEFAULT_SYSTEM_PROMPT,
        "disclaimer_text": script_mod.DEFAULT_DISCLAIMER,
        "placeholders": script_mod.PLACEHOLDERS,
    }


# =============================================================================
# System status / dashboard
# =============================================================================

@router.get("/system/status")
async def system_status():
    schema = await repo.schema_ready()
    runtime = await get_runtime()
    closers = await repo.list_closers() if schema else []
    agents = await repo.list_agents() if schema else []
    return {
        "database": "ok" if schema else "missing_tables",
        "telnyx": "ok" if TELNYX.configured else "missing_config",
        "webhook_signature_check": bool(settings.telnyx_webhook_public_key),
        "llm": f"{settings.llm_provider}:{settings.openai_model if settings.llm_provider == 'openai' else settings.anthropic_model}",
        "llm_key": bool(settings.openai_api_key if settings.llm_provider == "openai" else settings.anthropic_api_key),
        "cartesia_key": bool(settings.cartesia_api_key),
        "public_base_url": settings.public_base_url,
        "public_url_ok": settings.public_base_url.startswith("https://"),
        "google_service_account_email": sheets.service_account_email(),
        "dialer_running": dialer.running,
        "dialer_last_error": dialer.last_error,
        "dialer_paused_reason": dialer.paused_reason,
        "closers_ready": len([c for c in closers if c["enabled"] and (c.get("destination") or "").strip()]),
        "agents_ready": len([a for a in agents if a.get("enabled")]),
        "followups_pending": await repo.count_followups("pending") if schema else 0,
        "disclaimer_incomplete": script_mod.disclaimer_incomplete(runtime),
        "company_name_set": runtime["company_name"] != DEFAULTS["company_name"],
    }


def _talk_seconds(call: dict) -> int:
    value = call.get("talk_seconds")
    if value is None:
        value = call.get("duration_seconds") if call.get("answered_at") else 0
    return int(value or 0)


def _totals(calls: list[dict]) -> dict:
    answered = [c for c in calls if c.get("answered_at")]
    transferred = [c for c in calls if c.get("outcome") == "transferred"]
    talk = [_talk_seconds(c) for c in answered if _talk_seconds(c) > 0]
    return {
        "calls": len(calls),
        "answered": len(answered),
        "answer_rate": round(100 * len(answered) / len(calls), 1) if calls else 0,
        "transferred": len(transferred),
        "transfer_rate": round(100 * len(transferred) / len(answered), 1) if answered else 0,
        "avg_talk_seconds": round(sum(talk) / len(talk)) if talk else 0,
        "needs_closer_followup": len([c for c in calls if c.get("needs_closer_followup")]),
    }


@router.get("/dashboard")
async def dashboard(period: str = "today", agent_id: str = ""):
    """period = today | month. agent_id = one agent, or blank for all of them."""
    period = "month" if period == "month" else "today"
    since = await _range_start(period)
    calls = await repo.calls_since(since, agent_id=agent_id)
    by_outcome: dict[str, int] = {}
    for c in calls:
        if c.get("outcome"):
            by_outcome[c["outcome"]] = by_outcome.get(c["outcome"], 0) + 1
    active = await repo.active_calls()
    if agent_id:
        active = [c for c in active if c.get("agent_id") == agent_id]
    agents = await repo.list_agents()
    return {
        "calling": dialer.running,
        "paused_reason": dialer.paused_reason,
        "period": period,
        "agent_id": agent_id,
        "agents": [{"id": a["id"], "name": a["name"], "label": a.get("label") or "", "enabled": a.get("enabled")}
                   for a in agents],
        "leads": await repo.lead_status_counts(),
        "totals": _totals(calls),
        "by_outcome": by_outcome,
        "followups_pending": await repo.count_followups("pending"),
        "pending_callbacks": await repo.pending_callback_count(),
        "active_calls": [_call_view(c) for c in active],
        "closers": await repo.list_closers(),
    }


@router.get("/scorecards")
async def scorecards(period: str = "today"):
    """Per-agent and per-closer numbers for the selected period."""
    period = "month" if period == "month" else "today"
    since = await _range_start(period)
    calls = await repo.calls_since(since)
    attempts = await repo.attempts_since(since)

    agent_rows = []
    for agent in await repo.list_agents():
        mine = [c for c in calls if c.get("agent_id") == agent["id"]]
        agent_rows.append({"id": agent["id"], "name": agent["name"], "label": agent.get("label") or "",
                           "enabled": agent.get("enabled"), **_totals(mine)})
    unassigned = [c for c in calls if not c.get("agent_id")]
    if unassigned:
        agent_rows.append({"id": "", "name": "Not recorded", "label": "", "enabled": False, **_totals(unassigned)})

    closer_rows = []
    for closer in await repo.list_closers():
        mine = [a for a in attempts if a.get("closer_id") == closer["id"]]
        accepted = [a for a in mine if a["status"] == "connected"]
        missed = [a for a in mine if a["status"] in ("no_answer", "busy", "declined", "failed")]
        pickup = []
        for a in mine:
            if a.get("answered_at") and a.get("started_at"):
                try:
                    started = datetime.fromisoformat(a["started_at"].replace("Z", "+00:00"))
                    answered = datetime.fromisoformat(a["answered_at"].replace("Z", "+00:00"))
                    pickup.append(max(0, (answered - started).total_seconds()))
                except Exception:
                    pass
        talk = [_talk_seconds(c) for c in calls if c.get("closer_id") == closer["id"] and _talk_seconds(c) > 0]
        closer_rows.append({
            "id": closer["id"], "name": closer["name"], "enabled": closer.get("enabled"),
            "availability": closer.get("availability"), "status": closer.get("status"),
            "offered": len(mine), "accepted": len(accepted), "missed": len(missed),
            "accept_rate": round(100 * len(accepted) / len(mine), 1) if mine else 0,
            "avg_pickup_seconds": round(sum(pickup) / len(pickup), 1) if pickup else 0,
            "avg_talk_seconds": round(sum(talk) / len(talk)) if talk else 0,
            "total_transfers": closer.get("total_transfers") or 0,
        })
    return {"period": period, "agents": agent_rows, "closers": closer_rows}


def _call_view(call: dict) -> dict:
    started = call.get("answered_at") or call.get("started_at")
    elapsed = None
    if started and not call.get("ended_at"):
        try:
            elapsed = int((_now() - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())
        except Exception:
            elapsed = None
    return {**call, "elapsed_seconds": elapsed, "talk_seconds": _talk_seconds(call),
            "phone_pretty": pretty_phone(call.get("phone") or "")}


# =============================================================================
# Dialer
# =============================================================================

@router.post("/calling/start")
async def calling_start(user: dict = Depends(current_user)):
    if not TELNYX.configured:
        raise HTTPException(400, "Telnyx isn't configured in backend/.env (API key, connection ID, from number).")
    if not settings.public_base_url.startswith("https://"):
        raise HTTPException(400, "PUBLIC_BASE_URL isn't a public https:// address. Start the backend with start.py.")
    closers = [c for c in await repo.list_closers() if c["enabled"] and (c.get("destination") or "").strip()]
    if not closers:
        raise HTTPException(400, "Add at least one closer phone number on the Closers page before calling.")
    if not await repo.enabled_agents():
        raise HTTPException(400, "Switch on at least one AI agent on the Agents page before calling.")
    dialer.start(user["username"])
    await repo.audit(user["username"], "dialer_started")
    return {"running": True}


@router.post("/calling/stop")
async def calling_stop(user: dict = Depends(current_user)):
    dialer.stop(user["username"])
    await repo.audit(user["username"], "dialer_stopped")
    return {"running": False}


@router.get("/calling/status")
async def calling_status():
    return {"running": dialer.running, "active": await repo.count_active_calls(),
            "last_error": dialer.last_error, "paused_reason": dialer.paused_reason}


# =============================================================================
# Leads
# =============================================================================

def _lead_view(lead: dict, runtime: dict) -> dict:
    return {
        **lead,
        "phone_pretty": pretty_phone(lead["phone"]),
        "callable_now": hours.is_callable_now(lead, runtime),
        "has_consent": bool(lead.get("consent_at")),
    }


@router.get("/leads")
async def leads_list(q: str = "", status: str = "", page: int = 1, page_size: int = 50):
    offset, limit = _page(page, page_size)
    data, total = await repo.list_leads(q, status, offset, limit)
    runtime = await get_runtime()
    return {"items": [_lead_view(x, runtime) for x in data], "total": total, "page": page, "page_size": limit}


@router.get("/leads/{lead_id}")
async def lead_detail(lead_id: int):
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    runtime = await get_runtime()
    return {**_lead_view(lead, runtime), "calls": await repo.calls_for_lead(lead_id),
            "local_time": hours.local_time_description(lead, runtime)}


class LeadIn(BaseModel):
    first_name: str = ""
    last_name: str = ""
    phone: str
    email: str | None = None
    zip_code: str | None = None
    state: str | None = None
    source: str = ""
    notes: str = ""
    consent_source: str = ""
    consent_confirmed: bool = False


@router.post("/leads")
async def lead_create(body: LeadIn, user: dict = Depends(current_user)):
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "That phone number isn't valid.")
    if not body.consent_confirmed or len(body.consent_source.strip()) < 3:
        raise HTTPException(400, "Confirm this person gave permission to be contacted and say where it came from.")
    if await repo.get_leads_by_phones([phone]):
        raise HTTPException(409, "A lead with this phone number already exists.")
    if await repo.is_dnc(phone):
        raise HTTPException(400, "This number is on the do-not-call list.")
    lead = await repo.insert_lead({
        "first_name": body.first_name.strip(), "last_name": body.last_name.strip(), "phone": phone,
        "phone_raw": body.phone, "email": body.email or None,
        "zip_code": "".join(ch for ch in (body.zip_code or "") if ch.isdigit())[:5] or None,
        "state": (body.state or "").strip().upper() or None, "source": body.source.strip(), "notes": body.notes.strip(),
        "timezone": timezone_for_phone(phone), "consent_at": _now().isoformat(),
        "consent_source": body.consent_source.strip(), "status": "new",
    })
    await repo.audit(user["username"], "lead_created", {"lead_id": lead.get("id")})
    return lead


class LeadPatch(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    zip_code: str | None = None
    state: str | None = None
    source: str | None = None
    notes: str | None = None
    status: str | None = None


EDITABLE_LEAD_STATUSES = {"new", "not_interested", "not_qualified", "contacted", "failed", "no_answer_final",
                          "awaiting_closer"}


@router.patch("/leads/{lead_id}")
async def lead_update(lead_id: int, body: LeadPatch, user: dict = Depends(current_user)):
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if "status" in data:
        if data["status"] not in EDITABLE_LEAD_STATUSES:
            raise HTTPException(400, "That status can't be set by hand.")
        if lead["status"] in ("do_not_call", "calling"):
            raise HTTPException(400, "This lead's status can't be changed right now.")
        if data["status"] == "new":
            data.update(retry_count=0, next_attempt_at=None, attempt_counts={})
    await repo.update_lead(lead_id, data)
    await repo.audit(user["username"], "lead_updated", {"lead_id": lead_id, "fields": list(data)})
    return {"success": True}


@router.delete("/leads/{lead_id}")
async def lead_delete(lead_id: int, user: dict = Depends(current_user)):
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if lead.get("current_call_id"):
        raise HTTPException(400, "This lead is on a call right now.")
    await repo.delete_lead(lead_id)
    await repo.audit(user["username"], "lead_deleted", {"lead_id": lead_id, "phone": lead["phone"]})
    return {"success": True}


@router.post("/leads/{lead_id}/call")
async def lead_call_now(lead_id: int, user: dict = Depends(current_user)):
    try:
        call = await dialer.call_now(lead_id)
    except DialError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("call now failed")
        raise HTTPException(502, f"Couldn't place the call: {e}")
    await repo.audit(user["username"], "manual_call", {"lead_id": lead_id, "call_id": call.get("id")})
    return {"success": True, "call_id": call.get("id")}


@router.post("/leads/{lead_id}/dnc")
async def lead_mark_dnc(lead_id: int, user: dict = Depends(current_user)):
    lead = await repo.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    await repo.add_dnc([{"phone": lead["phone"], "reason": "marked from dashboard", "source": "manual"}])
    await repo.update_leads_by_phone(lead["phone"], {"status": "do_not_call", "next_attempt_at": None})
    for cb in await repo.pending_callbacks_for_lead(lead_id):
        await repo.update_callback(cb["id"], {"status": "cancelled", "result": "do_not_call"})
    await repo.audit(user["username"], "lead_dnc", {"lead_id": lead_id})
    return {"success": True}


# =============================================================================
# Calls — the Call History page is the one place calls are watched and managed
# =============================================================================

OUTCOME_LABELS = {
    "transferred": "Transferred",
    "no_closer_available": "Closer to call back",
    "callback_scheduled": "Callback booked",
    "not_interested": "Not interested",
    "not_qualified": "No Medicare",
    "do_not_call": "Asked not to be called",
    "wrong_number": "Wrong number",
    "completed": "Finished",
    "voicemail": "Voicemail",
    "no_answer": "No answer",
    "busy": "Line busy",
    "bad_number": "Bad number",
    "caller_hangup": "They hung up",
    "caller_hangup_early": "Hung up early",
    "failed": "Call failed",
    "no_conversation": "No conversation",
}

STATUS_LABELS = {
    "DIALING": "Dialing",
    "RINGING": "Ringing",
    "ANSWERED": "Answered",
    "AI_CONVERSATION": "On a call now",
    "TRANSFERRING": "Connecting a closer",
    "TRANSFERRED": "With the closer",
}


def _what_happened(call: dict) -> str:
    """One plain-English phrase per call, for the 'What happened' column."""
    if call.get("status") != "ENDED":
        if call.get("status") == "TRANSFERRED" and call.get("closer_name"):
            return f"With {call['closer_name']}"
        return STATUS_LABELS.get(call.get("status") or "", call.get("status") or "")
    outcome = call.get("outcome") or ""
    if outcome == "transferred" and call.get("closer_name"):
        return f"Transferred to {call['closer_name']}"
    return OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").capitalize() or "Finished")


async def _history_view(call: dict, callbacks: dict[str, dict]) -> dict:
    view = _call_view(call)
    view["what_happened"] = _what_happened(call)
    view["live"] = call.get("status") not in ("ENDED",)
    cb = callbacks.get(str(call.get("lead_id")))
    view["callback"] = cb
    return view


@router.get("/calls")
async def calls_list(outcome: str = "", q: str = "", agent_id: str = "", period: str = "",
                     followup: str = "", page: int = 1, page_size: int = 50):
    """outcome can also be 'live' (calls happening right now). period = today | month | blank for all time."""
    offset, limit = _page(page, page_size)
    since = await _range_start(period) if period in ("today", "month") else ""
    data, total = await repo.list_calls(outcome=outcome, q=q, agent_id=agent_id, since_iso=since,
                                        followup=followup, offset=offset, limit=limit)
    # Pull the booked callbacks for the leads on this page in one go.
    callbacks: dict[str, dict] = {}
    for cb in await repo.list_callbacks("pending", limit=500):
        callbacks[str(cb["lead_id"])] = {"id": cb["id"], "scheduled_for": cb["scheduled_for"],
                                         "reason": cb.get("reason") or "", "status": cb["status"]}
    items = [await _history_view(c, callbacks) for c in data]
    agents = await repo.list_agents()
    return {
        "items": items, "total": total, "page": page, "page_size": limit,
        "outcomes": [{"value": k, "label": v} for k, v in OUTCOME_LABELS.items()],
        "agents": [{"id": a["id"], "name": a["name"]} for a in agents],
        "live_count": await repo.count_active_calls(),
    }


def _call_or_404(call_id: str) -> str:
    try:
        uuid.UUID(call_id)
    except ValueError:
        raise HTTPException(404, "Call not found")
    return call_id


@router.get("/calls/{call_id}")
async def call_detail(call_id: str):
    call = await repo.get_call(_call_or_404(call_id))
    if not call:
        raise HTTPException(404, "Call not found")
    call.pop("stream_token", None)
    lead = await repo.get_lead(call["lead_id"]) if call.get("lead_id") else None
    runtime = await get_runtime()
    callbacks = []
    if call.get("lead_id"):
        for cb in await repo.pending_callbacks_for_lead(call["lead_id"]):
            callbacks.append({**cb, "local_time": hours.spoken_time(
                datetime.fromisoformat(cb["scheduled_for"].replace("Z", "+00:00")),
                lead or {"timezone": timezone_for_phone(call["phone"])}, runtime)})
    return {
        **_call_view(call),
        "what_happened": _what_happened(call),
        "lead": _lead_view(lead, runtime) if lead else None,
        "callbacks": callbacks,
        "transfer_attempts": await repo.attempts_for_call(call_id),
        "events": await repo.events_for_call(call_id),
    }


@router.get("/calls/{call_id}/recording")
async def call_recording(call_id: str):
    call = await repo.get_call(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if call.get("recording_id"):
        try:
            url = await TELNYX.recording_url(call["recording_id"])
            if url:
                return {"url": url}
        except Exception as e:
            logger.warning(f"recording lookup failed: {e}")
    if call.get("recording_url"):
        return {"url": call["recording_url"]}
    raise HTTPException(404, "No recording is available for this call yet.")


@router.post("/calls/{call_id}/hangup")
async def call_hangup(call_id: str, user: dict = Depends(current_user)):
    call = await repo.get_call(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if call.get("status") == "ENDED":
        return {"success": True}
    await outcomes.set_outcome(call_id, "completed")
    if call.get("telnyx_call_control_id"):
        await TELNYX.hangup(call["telnyx_call_control_id"])
    else:
        await lifecycle.finalize_call(call_id, "manual_hangup")
    await repo.audit(user["username"], "manual_hangup", {"call_id": call_id})
    return {"success": True}


# =============================================================================
# Leads waiting for a closer to call them back
# =============================================================================

@router.get("/followups")
async def followups_list(status: str = "pending"):
    status = status if status in ("pending", "done") else "pending"
    calls = await repo.followup_calls(status)
    runtime = await get_runtime()
    items = []
    for call in calls:
        view = _call_view(call)
        view["what_happened"] = _what_happened(call)
        view["local_time"] = hours.local_time_description(
            {"timezone": timezone_for_phone(call["phone"])}, runtime)
        items.append(view)
    return {"items": items, "pending": await repo.count_followups("pending"),
            "done": await repo.count_followups("done")}


class FollowupPatch(BaseModel):
    status: str  # done | pending


@router.patch("/followups/{call_id}")
async def followup_update(call_id: str, body: FollowupPatch, user: dict = Depends(current_user)):
    call = await repo.get_call(call_id)
    if not call or not call.get("needs_closer_followup"):
        raise HTTPException(404, "That call isn't on the closer call-back list.")
    if body.status not in ("done", "pending"):
        raise HTTPException(400, "status must be done or pending")
    await repo.update_call(call_id, {
        "followup_status": body.status,
        "followup_by": user["username"] if body.status == "done" else None,
        "followup_at": _now().isoformat() if body.status == "done" else None,
    })
    if body.status == "done" and call.get("lead_id"):
        lead = await repo.get_lead(call["lead_id"])
        if lead and lead["status"] == "awaiting_closer":
            await repo.update_lead(lead["id"], {"status": "contacted"})
    await repo.audit(user["username"], f"followup_{body.status}", {"call_id": call_id})
    return {"success": True}


@router.get("/followups.csv")
async def followups_csv(status: str = "pending", user: dict = Depends(current_user)):
    calls = await repo.followup_calls(status if status in ("pending", "done") else "pending")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Lead", "Phone", "ZIP", "State", "Agent", "When the AI called", "Has Part A & B",
                     "Current coverage", "Wants an agent", "Notes"])
    for c in calls:
        q = c.get("qualification") or {}
        writer.writerow([c.get("lead_name") or "", pretty_phone(c.get("phone") or ""), c.get("lead_zip") or "",
                         c.get("lead_state") or "", c.get("agent_name") or "", c.get("started_at") or "",
                         "yes" if q.get("has_medicare_parts_a_and_b") else "", q.get("current_coverage") or "",
                         "yes" if q.get("wants_licensed_agent") else "", q.get("notes") or ""])
    await repo.audit(user["username"], "followups_exported", {"count": len(calls)})
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="closer-callbacks.csv"'})


# =============================================================================
# Closers
# =============================================================================

class CloserIn(BaseModel):
    name: str = Field(min_length=1)
    destination: str = ""
    priority: int = 1
    enabled: bool = True


def _clean_destination(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.lower().startswith("sip:"):
        return value
    phone = normalize_phone(value)
    if not phone:
        raise HTTPException(400, "Closer destination must be a valid phone number or a sip: address.")
    return phone


@router.get("/closers")
async def closers_list():
    since = await _range_start("today")
    recent = await repo.calls_since(since)
    items = []
    for closer in await repo.list_closers():
        items.append({**closer, "transfers_today": len([c for c in recent if c.get("closer_id") == closer["id"]
                                                        and c.get("outcome") == "transferred"])})
    return {"items": items}


@router.post("/closers")
async def closer_create(body: CloserIn, user: dict = Depends(current_user)):
    closer = await repo.insert_closer({
        "name": body.name.strip(), "destination": _clean_destination(body.destination),
        "priority": max(1, body.priority), "enabled": body.enabled, "availability": "available", "status": "FREE",
    })
    await repo.audit(user["username"], "closer_created", {"closer_id": closer.get("id")})
    return closer


class CloserPatch(BaseModel):
    name: str | None = None
    destination: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    availability: str | None = None


@router.patch("/closers/{closer_id}")
async def closer_update(closer_id: str, body: CloserPatch, user: dict = Depends(current_user)):
    if not await repo.get_closer(closer_id):
        raise HTTPException(404, "Closer not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if "destination" in data:
        data["destination"] = _clean_destination(data["destination"])
    if "availability" in data and data["availability"] not in ("available", "away"):
        raise HTTPException(400, "availability must be available or away")
    if "priority" in data:
        data["priority"] = max(1, data["priority"])
    await repo.update_closer(closer_id, data)
    await repo.audit(user["username"], "closer_updated", {"closer_id": closer_id, **data})
    return {"success": True}


@router.delete("/closers/{closer_id}")
async def closer_delete(closer_id: str, user: dict = Depends(current_user)):
    closer = await repo.get_closer(closer_id)
    if not closer:
        raise HTTPException(404, "Closer not found")
    if closer["status"] != "FREE":
        raise HTTPException(400, "This closer is on a call right now.")
    await repo.delete_closer(closer_id)
    await repo.audit(user["username"], "closer_deleted", {"closer_id": closer_id, "name": closer["name"]})
    return {"success": True}


@router.post("/closers/{closer_id}/release")
async def closer_release(closer_id: str, user: dict = Depends(current_user)):
    await repo.release_closer(closer_id)
    await repo.audit(user["username"], "closer_force_released", {"closer_id": closer_id})
    return {"success": True}


# =============================================================================
# Callbacks — no page of their own; these serve the Call History row actions
# =============================================================================

class CallbackPatch(BaseModel):
    status: str | None = None  # completed | cancelled
    scheduled_for: str | None = None  # ISO datetime


@router.patch("/callbacks/{cb_id}")
async def callback_update(cb_id: str, body: CallbackPatch, user: dict = Depends(current_user)):
    cb = await repo.get_callback(cb_id)
    if not cb:
        raise HTTPException(404, "Callback not found")
    lead = await repo.get_lead(cb["lead_id"]) if cb.get("lead_id") else None
    if body.scheduled_for:
        try:
            when = datetime.fromisoformat(body.scheduled_for.replace("Z", "+00:00"))
            if not when.tzinfo:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, "Invalid date/time.")
        if not lead:
            raise HTTPException(400, "The lead for this callback no longer exists.")
        await repo.update_callback(cb_id, {"status": "cancelled", "result": "rescheduled"})
        booked = await outcomes.schedule_callback(lead, None, when, cb.get("reason") or "rescheduled")
        await repo.audit(user["username"], "callback_rescheduled", {"callback_id": cb_id, "to": booked.isoformat()})
        return {"success": True, "scheduled_for": booked.isoformat()}
    if body.status in ("completed", "cancelled"):
        await repo.update_callback(cb_id, {"status": body.status, "completed_at": _now().isoformat(),
                                           "result": f"marked {body.status} by {user['username']}"})
        if lead and lead["status"] == "callback_scheduled" and not await repo.pending_callbacks_for_lead(lead["id"]):
            await repo.update_lead(lead["id"], {"status": "contacted" if body.status == "completed" else "not_interested",
                                                "next_attempt_at": None})
        await repo.audit(user["username"], f"callback_{body.status}", {"callback_id": cb_id})
        return {"success": True}
    raise HTTPException(400, "Nothing to update.")


# =============================================================================
# Do-not-call list
# =============================================================================

@router.get("/dnc")
async def dnc_list(q: str = "", page: int = 1, page_size: int = 100):
    offset, limit = _page(page, page_size)
    items, total = await repo.list_dnc(q, offset, limit)
    return {"items": [{**x, "phone_pretty": pretty_phone(x["phone"])} for x in items], "total": total}


class DncIn(BaseModel):
    phones: str  # one or more numbers, separated by commas/new lines
    reason: str = ""


@router.post("/dnc")
async def dnc_add(body: DncIn, user: dict = Depends(current_user)):
    raw = [p for chunk in body.phones.replace(",", "\n").splitlines() if (p := chunk.strip())]
    valid = sorted({n for p in raw if (n := normalize_phone(p))})
    if not valid:
        raise HTTPException(400, "No valid phone numbers found.")
    await _add_dnc_numbers(valid, body.reason or "added manually", "manual")
    await repo.audit(user["username"], "dnc_added", {"count": len(valid)})
    return {"added": len(valid), "invalid": len(raw) - len(valid)}


@router.post("/dnc/upload")
async def dnc_upload(file: UploadFile = File(...), user: dict = Depends(current_user)):
    try:
        headers, data = lead_import.read_table(file.filename or "", await file.read())
    except ValueError as e:
        raise HTTPException(400, str(e))
    column = lead_import.detect_mapping(headers).get("phone") or (headers[0] if headers else None)
    numbers = sorted({n for row in data if (n := normalize_phone(row.get(column, "")))}) if column else []
    if not numbers:
        raise HTTPException(400, "No valid phone numbers found in that file.")
    await _add_dnc_numbers(numbers, f"uploaded from {file.filename}", "upload")
    await repo.audit(user["username"], "dnc_uploaded", {"count": len(numbers), "file": file.filename})
    return {"added": len(numbers), "rows": len(data)}


async def _add_dnc_numbers(numbers: list[str], reason: str, source: str) -> None:
    await repo.add_dnc([{"phone": n, "reason": reason, "source": source} for n in numbers])
    for n in numbers:
        await repo.update_leads_by_phone(n, {"status": "do_not_call", "next_attempt_at": None})


@router.delete("/dnc/{phone}")
async def dnc_remove(phone: str, user: dict = Depends(current_user)):
    normalized = normalize_phone(phone) or phone
    await repo.remove_dnc(normalized)
    await repo.audit(user["username"], "dnc_removed", {"phone": normalized})
    return {"success": True, "note": "Leads with this number stay marked do-not-call until you change them."}


# =============================================================================
# Imports (file + Google Sheet)
# =============================================================================

@router.post("/import/preview")
async def import_preview(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "File is larger than 25 MB.")
    try:
        headers, rows = lead_import.read_table(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Couldn't read that file: {e}")
    if not rows:
        raise HTTPException(400, "The file has no data rows.")
    return lead_import.create_preview("file", file.filename or "upload", headers, rows)


class ImportCommit(BaseModel):
    token: str
    mapping: dict[str, str | None]
    consent_mode: str  # column | attest
    attest: bool = False
    attest_source: str = ""


@router.post("/import/commit")
async def import_commit(body: ImportCommit, user: dict = Depends(current_user)):
    try:
        result = await lead_import.commit_preview(body.token, body.mapping, body.consent_mode, body.attest,
                                                  body.attest_source, user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    await repo.audit(user["username"], "leads_imported", result)
    return result


@router.get("/import/history")
async def import_history():
    return {"items": await repo.list_import_runs()}


@router.get("/sheets/status")
async def sheet_status():
    sheet = await repo.get_sheet() or {}
    return {**sheet, "service_account_email": sheets.service_account_email()}


class SheetConnect(BaseModel):
    url: str


@router.post("/sheets/preview")
async def sheet_preview(body: SheetConnect, user: dict = Depends(current_user)):
    sheet_id = sheets.extract_sheet_id(body.url)
    if not sheet_id:
        raise HTTPException(400, "Paste the Google Sheet link.")
    try:
        headers, rows, mode = await sheets.fetch_sheet(sheet_id)
    except sheets.SheetError as e:
        await repo.save_sheet({"url": body.url, "sheet_id": sheet_id, "status": "ERROR"})
        raise HTTPException(400, str(e))
    if not rows:
        raise HTTPException(400, "The sheet has no data rows.")
    await repo.save_sheet({"url": body.url, "sheet_id": sheet_id, "status": "CONNECTED", "last_sync": _now().isoformat()})
    preview = lead_import.create_preview("google_sheet", body.url, headers, rows)
    return {**preview, "access_mode": mode}


# =============================================================================
# Audit log (super admin only)
# =============================================================================

@router.get("/audit")
async def audit_list(q: str = "", action: str = "", period: str = "", page: int = 1, page_size: int = 100,
                     _: dict = Depends(require_super_admin)):
    offset, limit = _page(page, page_size)
    since = await _range_start(period) if period in ("today", "month") else ""
    items, total = await repo.list_audit(q=q, action=action, since_iso=since, offset=offset, limit=limit)
    return {"items": items, "total": total, "page": page, "page_size": limit}


# =============================================================================
# Settings
# =============================================================================

@router.get("/settings")
async def settings_get(user: dict = Depends(current_user)):
    return {"values": await get_runtime(force=True), "defaults": DEFAULTS,
            "super_admin_keys": sorted(SUPER_ADMIN_KEYS), "can_edit_all": is_super_admin(user)}


@router.put("/settings")
async def settings_save(body: dict[str, Any], user: dict = Depends(current_user)):
    restricted = sorted(set(body) & SUPER_ADMIN_KEYS)
    if restricted and not is_super_admin(user):
        raise HTTPException(403, "Only the super admin can change the dialer, retry, transfer and safety settings.")
    try:
        values = await save_runtime(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await repo.audit(user["username"], "settings_updated", {"keys": sorted(body.keys())})
    return {"values": values}
