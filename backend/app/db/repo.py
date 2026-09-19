"""
All database reads/writes. Nothing else in the app talks to Supabase directly,
so the rest of the code (and the tests) only need to know these functions.
"""

from datetime import datetime, timezone
from typing import Any

from app.db.client import first, rows, run

IN_PROGRESS_CALL_STATUSES = ["DIALING", "RINGING", "ANSWERED", "AI_CONVERSATION", "TRANSFERRING", "TRANSFERRED"]
DIALABLE_LEAD_STATUSES = ["new", "retry_scheduled", "callback_scheduled"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _search_terms(q: str) -> tuple[str, str]:
    """Clean a search box value for PostgREST filters.

    Uses '*' as the wildcard (PostgREST's URL-safe form of '%'): a literal '%' followed by two
    hex digits — e.g. searching '867…' — would be read as a URL escape and break the query.
    Returns (text term, digits-only term for phone matching).
    """
    text = "".join(ch for ch in (q or "") if ch not in ",()%*:\\\"'").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return text, digits


# =============================================================================
# Health
# =============================================================================

async def schema_ready() -> bool:
    try:
        await run(lambda c: c.table("app_schema_version").select("version").limit(1).execute())
        return True
    except Exception:
        return False


# =============================================================================
# Users
# =============================================================================

async def get_user_by_username(username: str) -> dict | None:
    return first(await run(lambda c: c.table("users").select("*").eq("username", username).limit(1).execute()))


async def get_user_by_id(user_id: str) -> dict | None:
    return first(await run(lambda c: c.table("users").select("*").eq("id", user_id).limit(1).execute()))


async def count_users() -> int:
    resp = await run(lambda c: c.table("users").select("id", count="exact", head=True).execute())
    return resp.count or 0


async def insert_user(data: dict) -> dict:
    return first(await run(lambda c: c.table("users").insert(data).execute())) or {}


async def update_user(user_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("users").update(data).eq("id", user_id).execute()))


async def list_users() -> list[dict]:
    return rows(await run(lambda c: c.table("users")
                          .select("id,username,full_name,role,enabled,created_by,created_at,last_login_at")
                          .order("created_at").execute()))


async def delete_user(user_id: str) -> None:
    await run(lambda c: c.table("users").delete().eq("id", user_id).execute())


async def count_users_with_role(role: str) -> int:
    resp = await run(lambda c: c.table("users").select("id", count="exact", head=True).eq("role", role).execute())
    return resp.count or 0


# =============================================================================
# AI agents
# =============================================================================

async def list_agents() -> list[dict]:
    return rows(await run(lambda c: c.table("agents").select("*").order("priority").order("name").execute()))


async def get_agent(agent_id: str) -> dict | None:
    return first(await run(lambda c: c.table("agents").select("*").eq("id", agent_id).limit(1).execute()))


async def count_agents() -> int:
    resp = await run(lambda c: c.table("agents").select("id", count="exact", head=True).execute())
    return resp.count or 0


async def insert_agent(data: dict) -> dict:
    return first(await run(lambda c: c.table("agents").insert(data).execute())) or {}


async def update_agent(agent_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("agents").update(data).eq("id", agent_id).execute()))


async def delete_agent(agent_id: str) -> None:
    await run(lambda c: c.table("agents").delete().eq("id", agent_id).execute())


async def enabled_agents() -> list[dict]:
    return rows(await run(lambda c: c.table("agents").select("*").eq("enabled", True)
                          .order("priority").order("name").execute()))


async def count_active_calls_for_agent(agent_id: str) -> int:
    resp = await run(lambda c: c.table("calls").select("id", count="exact", head=True)
                     .eq("agent_id", agent_id).in_("status", IN_PROGRESS_CALL_STATUSES).execute())
    return resp.count or 0


# =============================================================================
# Leads
# =============================================================================

async def list_leads(q: str = "", status: str = "", offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
    def query(c):
        qry = c.table("leads").select("*", count="exact")
        if status:
            qry = qry.eq("status", status)
        text, digits = _search_terms(q)
        if text:
            parts = [f"first_name.ilike.*{text}*", f"last_name.ilike.*{text}*", f"zip_code.ilike.*{text}*"]
            if len(digits) >= 3:
                parts.append(f"phone.ilike.*{digits}*")
            if " " in text:
                first, _, last = text.partition(" ")
                parts.append(f"and(first_name.ilike.*{first}*,last_name.ilike.*{last.strip()}*)")
            qry = qry.or_(",".join(parts))
        return qry.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

    resp = await run(query)
    return rows(resp), resp.count or 0


async def get_lead(lead_id: int) -> dict | None:
    return first(await run(lambda c: c.table("leads").select("*").eq("id", int(lead_id)).limit(1).execute()))


async def get_leads_by_phones(phones: list[str]) -> list[dict]:
    if not phones:
        return []
    out: list[dict] = []
    for i in range(0, len(phones), 200):
        chunk = phones[i:i + 200]
        out += rows(await run(lambda c, chunk=chunk: c.table("leads").select("id,phone").in_("phone", chunk).execute()))
    return out


async def insert_lead(data: dict) -> dict:
    return first(await run(lambda c: c.table("leads").insert(data).execute())) or {}


async def insert_leads(batch: list[dict]) -> int:
    count = 0
    for i in range(0, len(batch), 200):
        chunk = batch[i:i + 200]
        count += len(rows(await run(lambda c, chunk=chunk: c.table("leads").insert(chunk).execute())))
    return count


async def update_lead(lead_id: int, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("leads").update(data).eq("id", int(lead_id)).execute()))


async def update_leads_by_phone(phone: str, data: dict) -> None:
    await run(lambda c: c.table("leads").update(data).eq("phone", phone).execute())


async def delete_lead(lead_id: int) -> None:
    await run(lambda c: c.table("leads").delete().eq("id", int(lead_id)).execute())


async def dialable_lead_candidates(now: str, limit: int = 100) -> list[dict]:
    """Leads the dialer may call right now (before per-lead calling-hours/DNC filtering).

    Due callbacks come first, then scheduled retries/new leads oldest-first.
    """
    def base(c):
        return (
            c.table("leads").select("*")
            .not_.is_("consent_at", "null")
            .is_("current_call_id", "null")
        )

    callbacks = rows(await run(lambda c: base(c)
                               .eq("status", "callback_scheduled").lte("next_attempt_at", now)
                               .order("next_attempt_at").limit(limit).execute()))
    retries = rows(await run(lambda c: base(c)
                             .eq("status", "retry_scheduled").lte("next_attempt_at", now)
                             .order("next_attempt_at").limit(limit).execute()))
    new = rows(await run(lambda c: base(c)
                         .eq("status", "new")
                         .order("created_at").limit(limit).execute()))
    return callbacks + retries + new


async def claim_lead(lead_id: int, expected_status: str) -> dict | None:
    """Atomically move a lead into 'calling'. Returns None if someone else claimed it first."""
    return first(await run(lambda c: c.table("leads")
                           .update({"status": "calling"})
                           .eq("id", int(lead_id)).eq("status", expected_status)
                           .is_("current_call_id", "null")
                           .execute()))


async def queue_leads(limit: int = 200) -> list[dict]:
    return rows(await run(lambda c: c.table("leads").select("*")
                          .in_("status", DIALABLE_LEAD_STATUSES)
                          .order("next_attempt_at", nullsfirst=True)
                          .order("created_at").limit(limit).execute()))


# =============================================================================
# Closers
# =============================================================================

async def list_closers() -> list[dict]:
    return rows(await run(lambda c: c.table("closers").select("*").order("priority").order("name").execute()))


async def get_closer(closer_id: str) -> dict | None:
    return first(await run(lambda c: c.table("closers").select("*").eq("id", closer_id).limit(1).execute()))


async def count_closers() -> int:
    resp = await run(lambda c: c.table("closers").select("id", count="exact", head=True).execute())
    return resp.count or 0


async def insert_closer(data: dict) -> dict:
    return first(await run(lambda c: c.table("closers").insert(data).execute())) or {}


async def update_closer(closer_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("closers").update(data).eq("id", closer_id).execute()))


async def delete_closer(closer_id: str) -> None:
    await run(lambda c: c.table("closers").delete().eq("id", closer_id).execute())


async def available_closers(exclude_ids: list[str]) -> list[dict]:
    """Closers that can take a transfer right now, best first:
    enabled + marked available + not ringing/on a call + has a destination,
    ordered by priority, then whoever was assigned least recently (fair rotation)."""
    data = rows(await run(lambda c: c.table("closers").select("*")
                          .eq("enabled", True).eq("availability", "available").eq("status", "FREE")
                          .order("priority").order("last_assigned_at", nullsfirst=True)
                          .execute()))
    return [x for x in data if x["id"] not in exclude_ids and (x.get("destination") or "").strip()]


async def reserve_closer(closer_id: str, call_id: str) -> dict | None:
    """Atomically FREE -> RINGING. None means another call grabbed this closer first."""
    return first(await run(lambda c: c.table("closers")
                           .update({"status": "RINGING", "current_call_id": call_id, "busy_since": now_iso()})
                           .eq("id", closer_id).eq("status", "FREE").execute()))


async def release_closer(closer_id: str, only_if_call_id: str | None = None) -> None:
    def query(c):
        q = c.table("closers").update({"status": "FREE", "current_call_id": None, "busy_since": None}).eq("id", closer_id)
        if only_if_call_id:
            q = q.eq("current_call_id", only_if_call_id)
        return q.execute()

    await run(query)


async def stuck_closers(older_than_iso: str) -> list[dict]:
    return rows(await run(lambda c: c.table("closers").select("*")
                          .neq("status", "FREE").lt("busy_since", older_than_iso).execute()))


# =============================================================================
# Calls
# =============================================================================

async def insert_call(data: dict) -> dict:
    return first(await run(lambda c: c.table("calls").insert(data).execute())) or {}


async def get_call(call_id: str) -> dict | None:
    return first(await run(lambda c: c.table("calls").select("*").eq("id", call_id).limit(1).execute()))


async def get_call_by_ccid(ccid: str) -> dict | None:
    return first(await run(lambda c: c.table("calls").select("*").eq("telnyx_call_control_id", ccid).limit(1).execute()))


async def update_call(call_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("calls").update(data).eq("id", call_id).execute()))


async def transition_call(call_id: str, from_statuses: list[str], data: dict) -> dict | None:
    """Update a call only if it is still in one of from_statuses (guards against racing events)."""
    return first(await run(lambda c: c.table("calls").update(data).eq("id", call_id).in_("status", from_statuses).execute()))


async def active_calls() -> list[dict]:
    return rows(await run(lambda c: c.table("calls").select("*")
                          .in_("status", IN_PROGRESS_CALL_STATUSES)
                          .order("started_at", desc=True).limit(200).execute()))


async def count_active_calls() -> int:
    resp = await run(lambda c: c.table("calls").select("id", count="exact", head=True)
                     .in_("status", IN_PROGRESS_CALL_STATUSES).execute())
    return resp.count or 0


CALL_LIST_COLUMNS = (
    "id,lead_id,lead_name,phone,lead_zip,lead_state,lead_source,agent_id,agent_name,status,outcome,ai_state,"
    "closer_name,closer_id,is_callback,callback_id,retry_number,needs_closer_followup,followup_status,followup_by,"
    "followup_at,started_at,answered_at,transferred_at,ended_at,duration_seconds,talk_seconds,hangup_cause,"
    "amd_result,qualification"
)


async def list_calls(outcome: str = "", q: str = "", agent_id: str = "", since_iso: str = "",
                     live_only: bool = False, followup: str = "", offset: int = 0,
                     limit: int = 50) -> tuple[list[dict], int]:
    """The Call History table: one query with everything the page shows."""
    def query(c):
        qry = c.table("calls").select(CALL_LIST_COLUMNS, count="exact")
        if outcome == "live":
            qry = qry.in_("status", IN_PROGRESS_CALL_STATUSES)
        elif outcome:
            qry = qry.eq("outcome", outcome)
        if live_only:
            qry = qry.in_("status", IN_PROGRESS_CALL_STATUSES)
        if agent_id:
            qry = qry.eq("agent_id", agent_id)
        if since_iso:
            qry = qry.gte("started_at", since_iso)
        if followup:
            qry = qry.eq("needs_closer_followup", True).eq("followup_status", followup)
        text, digits = _search_terms(q)
        if text:
            parts = [f"lead_name.ilike.*{text}*", f"lead_zip.ilike.*{text}*", f"agent_name.ilike.*{text}*",
                     f"closer_name.ilike.*{text}*"]
            if len(digits) >= 3:
                parts.append(f"phone.ilike.*{digits}*")
            qry = qry.or_(",".join(parts))
        return qry.order("started_at", desc=True).range(offset, offset + limit - 1).execute()

    resp = await run(query)
    return rows(resp), resp.count or 0


async def count_followups(status: str = "pending") -> int:
    resp = await run(lambda c: c.table("calls").select("id", count="exact", head=True)
                     .eq("needs_closer_followup", True).eq("followup_status", status).execute())
    return resp.count or 0


async def followup_calls(status: str = "pending", limit: int = 500) -> list[dict]:
    return rows(await run(lambda c: c.table("calls").select(CALL_LIST_COLUMNS)
                          .eq("needs_closer_followup", True).eq("followup_status", status)
                          .order("started_at", desc=True).limit(limit).execute()))


async def calls_for_lead(lead_id: int, limit: int = 20) -> list[dict]:
    return rows(await run(lambda c: c.table("calls").select("id,status,outcome,started_at,duration_seconds,talk_seconds,closer_name,agent_name")
                          .eq("lead_id", int(lead_id)).order("started_at", desc=True).limit(limit).execute()))


async def stale_calls(statuses: list[str], older_than_iso: str) -> list[dict]:
    return rows(await run(lambda c: c.table("calls").select("*")
                          .in_("status", statuses).lt("started_at", older_than_iso).execute()))


async def calls_since(since_iso: str, agent_id: str = "") -> list[dict]:
    def query(c):
        q = (c.table("calls")
             .select("id,status,outcome,duration_seconds,talk_seconds,closer_id,closer_name,agent_id,agent_name,"
                     "answered_at,started_at,needs_closer_followup,followup_status")
             .gte("started_at", since_iso))
        if agent_id:
            q = q.eq("agent_id", agent_id)
        return q.order("started_at", desc=True).limit(20000).execute()

    return rows(await run(query))


async def recent_finished_calls(limit: int = 50) -> list[dict]:
    """Newest finished calls — used by the safety brake to spot a run of failures."""
    return rows(await run(lambda c: c.table("calls").select("id,outcome,answered_at,ended_at,started_at")
                          .eq("status", "ENDED").order("ended_at", desc=True).limit(limit).execute()))


async def attempts_since(since_iso: str) -> list[dict]:
    """Every closer transfer attempt since a moment — used for the closer scorecards."""
    return rows(await run(lambda c: c.table("transfer_attempts").select("*")
                          .gte("started_at", since_iso).order("started_at", desc=True).limit(20000).execute()))


# =============================================================================
# Call events
# =============================================================================

async def insert_call_event(data: dict) -> bool:
    """Returns False if this event_id was already stored (duplicate webhook delivery)."""
    existing = first(await run(lambda c: c.table("call_events").select("id").eq("event_id", data["event_id"]).limit(1).execute()))
    if existing:
        return False
    try:
        await run(lambda c: c.table("call_events").insert(data).execute())
        return True
    except Exception:
        return False  # unique violation from a concurrent duplicate


async def events_for_call(call_id: str) -> list[dict]:
    return rows(await run(lambda c: c.table("call_events").select("event_type,created_at,payload")
                          .eq("call_id", call_id).order("created_at").limit(500).execute()))


# =============================================================================
# Transfer attempts
# =============================================================================

async def insert_attempt(data: dict) -> dict:
    return first(await run(lambda c: c.table("transfer_attempts").insert(data).execute())) or {}


async def get_attempt(attempt_id: str) -> dict | None:
    return first(await run(lambda c: c.table("transfer_attempts").select("*").eq("id", attempt_id).limit(1).execute()))


async def update_attempt(attempt_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("transfer_attempts").update(data).eq("id", attempt_id).execute()))


async def attempts_for_call(call_id: str) -> list[dict]:
    return rows(await run(lambda c: c.table("transfer_attempts").select("*")
                          .eq("call_id", call_id).order("started_at").execute()))


# =============================================================================
# Callbacks
# =============================================================================

async def insert_callback(data: dict) -> dict:
    return first(await run(lambda c: c.table("callbacks").insert(data).execute())) or {}


async def get_callback(cb_id: str) -> dict | None:
    return first(await run(lambda c: c.table("callbacks").select("*").eq("id", cb_id).limit(1).execute()))


async def update_callback(cb_id: str, data: dict) -> dict | None:
    return first(await run(lambda c: c.table("callbacks").update(data).eq("id", cb_id).execute()))


async def pending_callbacks_for_lead(lead_id: int) -> list[dict]:
    return rows(await run(lambda c: c.table("callbacks").select("*")
                          .eq("lead_id", int(lead_id)).in_("status", ["pending", "dialing"]).execute()))


async def list_callbacks(status: str = "", limit: int = 300) -> list[dict]:
    def query(c):
        q = c.table("callbacks").select("*")
        if status:
            q = q.eq("status", status)
        return q.order("scheduled_for").limit(limit).execute()

    return rows(await run(query))


# =============================================================================
# DNC
# =============================================================================

async def is_dnc(phone: str) -> bool:
    return first(await run(lambda c: c.table("dnc_numbers").select("phone").eq("phone", phone).limit(1).execute())) is not None


async def dnc_among(phones: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(phones), 200):
        chunk = phones[i:i + 200]
        out |= {r["phone"] for r in rows(await run(lambda c, chunk=chunk: c.table("dnc_numbers").select("phone").in_("phone", chunk).execute()))}
    return out


async def add_dnc(entries: list[dict]) -> None:
    for i in range(0, len(entries), 200):
        chunk = entries[i:i + 200]
        await run(lambda c, chunk=chunk: c.table("dnc_numbers").upsert(chunk, on_conflict="phone").execute())


async def remove_dnc(phone: str) -> None:
    await run(lambda c: c.table("dnc_numbers").delete().eq("phone", phone).execute())


async def list_dnc(q: str = "", offset: int = 0, limit: int = 100) -> tuple[list[dict], int]:
    def query(c):
        qry = c.table("dnc_numbers").select("*", count="exact")
        _, digits = _search_terms(q)
        if digits:
            qry = qry.ilike("phone", f"*{digits}*")
        return qry.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

    resp = await run(query)
    return rows(resp), resp.count or 0


# =============================================================================
# Agent script / system config / sheets / imports / audit
# =============================================================================

async def get_dialer_state() -> dict:
    row = first(await run(lambda c: c.table("system_config").select("config").eq("id", "dialer_state").limit(1).execute()))
    return (row or {}).get("config") or {}


async def save_dialer_state(state: dict) -> None:
    await run(lambda c: c.table("system_config").upsert(
        {"id": "dialer_state", "config": state, "updated_at": now_iso()}, on_conflict="id").execute())


async def get_system_config() -> dict:
    row = first(await run(lambda c: c.table("system_config").select("config").eq("id", "runtime").limit(1).execute()))
    return (row or {}).get("config") or {}


async def save_system_config(config: dict) -> None:
    await run(lambda c: c.table("system_config").upsert(
        {"id": "runtime", "config": config, "updated_at": now_iso()}, on_conflict="id").execute())


async def get_sheet() -> dict | None:
    return first(await run(lambda c: c.table("connected_sheets").select("*").eq("id", "default").limit(1).execute()))


async def save_sheet(data: dict) -> None:
    payload = {**data, "id": "default", "updated_at": now_iso()}
    await run(lambda c: c.table("connected_sheets").upsert(payload, on_conflict="id").execute())


async def insert_import_run(data: dict) -> None:
    await run(lambda c: c.table("import_runs").insert(data).execute())


async def list_import_runs(limit: int = 20) -> list[dict]:
    return rows(await run(lambda c: c.table("import_runs").select("*").order("created_at", desc=True).limit(limit).execute()))


async def audit(username: str | None, action: str, details: dict[str, Any] | None = None) -> None:
    try:
        await run(lambda c: c.table("audit_logs").insert(
            {"username": username, "action": action, "details": details or {}}).execute())
    except Exception:
        pass  # auditing must never break the action being audited


async def list_audit(q: str = "", action: str = "", since_iso: str = "", offset: int = 0,
                     limit: int = 100) -> tuple[list[dict], int]:
    def query(c):
        qry = c.table("audit_logs").select("*", count="exact")
        if action:
            qry = qry.eq("action", action)
        if since_iso:
            qry = qry.gte("created_at", since_iso)
        text, _ = _search_terms(q)
        if text:
            qry = qry.or_(f"username.ilike.*{text}*,action.ilike.*{text}*")
        return qry.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

    resp = await run(query)
    return rows(resp), resp.count or 0


# =============================================================================
# Dashboard counts
# =============================================================================

async def lead_status_counts() -> dict[str, int]:
    statuses = ["new", "calling", "retry_scheduled", "callback_scheduled", "awaiting_closer", "transferred",
                "not_interested", "not_qualified", "do_not_call", "no_answer_final", "wrong_number",
                "contacted", "failed"]
    out: dict[str, int] = {}
    for s in statuses:
        resp = await run(lambda c, s=s: c.table("leads").select("id", count="exact", head=True).eq("status", s).execute())
        out[s] = resp.count or 0
    total = await run(lambda c: c.table("leads").select("id", count="exact", head=True).execute())
    no_consent = await run(lambda c: c.table("leads").select("id", count="exact", head=True).is_("consent_at", "null").execute())
    out["total"] = total.count or 0
    out["missing_consent"] = no_consent.count or 0
    return out


async def pending_callback_count() -> int:
    resp = await run(lambda c: c.table("callbacks").select("id", count="exact", head=True).eq("status", "pending").execute())
    return resp.count or 0
