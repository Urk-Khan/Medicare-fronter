"""In-memory stand-ins for the database and Telnyx so call flows can be tested end to end."""

import uuid
from datetime import datetime, timezone

from app.db import repo as repo_module


def _now():
    return datetime.now(timezone.utc).isoformat()


class FakeDB:
    def __init__(self):
        self.leads: dict[int, dict] = {}
        self.calls: dict[str, dict] = {}
        self.closers: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}
        self.callbacks: dict[str, dict] = {}
        self.dnc: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.events: set[str] = set()
        self.audits: list[dict] = []
        self.dialer_state: dict = {}
        self.config: dict = {}

    # ---- helpers for tests
    def add_lead(self, **kw):
        lid = len(self.leads) + 1
        lead = {"id": lid, "first_name": "Mary", "last_name": "Johnson", "phone": f"+1415867{2670 + lid}",
                "timezone": "America/New_York", "status": "new", "retry_count": 0, "qualification": {},
                "consent_at": _now(), "current_call_id": None, "next_attempt_at": None,
                "attempt_counts": {}, "zip_code": "33101", "state": "FL", "source": "web form", **kw}
        self.leads[lid] = lead
        return lead

    def add_agent(self, name="Ava", priority=1, **kw):
        aid = str(uuid.uuid4())
        self.agents[aid] = {"id": aid, "name": name, "label": "", "enabled": True, "voice_id": "", "from_number": "",
                            "max_concurrent_calls": 3, "priority": priority, "opening_line": "Hi {{lead_first_name}}",
                            "system_prompt": "You are {{agent_name}}", "disclaimer_text": "We do not offer every plan",
                            "total_calls": 0, **kw}
        return self.agents[aid]

    def add_closer(self, name, destination, priority=1, **kw):
        cid = str(uuid.uuid4())
        self.closers[cid] = {"id": cid, "name": name, "destination": destination, "priority": priority,
                             "enabled": True, "availability": "available", "status": "FREE", "current_call_id": None,
                             "last_assigned_at": None, "total_transfers": 0, **kw}
        return self.closers[cid]

    def add_call(self, lead, **kw):
        cid = str(uuid.uuid4())
        self.calls[cid] = {"id": cid, "lead_id": lead["id"], "phone": lead["phone"], "status": "AI_CONVERSATION",
                           "telnyx_call_control_id": f"lead-leg-{cid[:6]}", "outcome": None, "answered_at": _now(),
                           "started_at": _now(), "ended_at": None, "qualification": {}, "is_callback": False,
                           "amd_result": None, "needs_closer_followup": False, "followup_status": None,
                           "agent_id": None, "agent_name": "Ava", **kw}
        lead["current_call_id"] = cid
        return self.calls[cid]

    # ---- install as the repo module
    def install(self, monkeypatch):
        db = self

        async def get_lead(lid): return db.leads.get(int(lid))
        async def update_lead(lid, data):
            db.leads[int(lid)].update(data); return db.leads[int(lid)]
        async def update_leads_by_phone(phone, data):
            for lead in db.leads.values():
                if lead["phone"] == phone:
                    lead.update(data)
        async def get_call(cid): return db.calls.get(cid)
        async def get_call_by_ccid(ccid): return next((c for c in db.calls.values() if c.get("telnyx_call_control_id") == ccid), None)
        async def update_call(cid, data):
            if cid in db.calls:
                db.calls[cid].update(data); return db.calls[cid]
        async def transition_call(cid, statuses, data):
            call = db.calls.get(cid)
            if call and call["status"] in statuses:
                call.update(data); return call
            return None
        async def insert_call(data):
            cid = str(uuid.uuid4()); db.calls[cid] = {"id": cid, "started_at": _now(), "ended_at": None, "outcome": None, **data}
            return db.calls[cid]
        async def get_closer(cid): return db.closers.get(cid)
        async def update_closer(cid, data): db.closers[cid].update(data); return db.closers[cid]
        async def available_closers(exclude_ids):
            items = [c for c in db.closers.values() if c["enabled"] and c["availability"] == "available"
                     and c["status"] == "FREE" and c["id"] not in exclude_ids and c["destination"]]
            return sorted(items, key=lambda c: (c["priority"], c["last_assigned_at"] or ""))
        async def reserve_closer(cid, call_id):
            c = db.closers[cid]
            if c["status"] != "FREE":
                return None
            c.update(status="RINGING", current_call_id=call_id); return c
        async def release_closer(cid, only_if_call_id=None):
            c = db.closers.get(cid)
            if c and (only_if_call_id is None or c["current_call_id"] == only_if_call_id):
                c.update(status="FREE", current_call_id=None, busy_since=None)
        async def insert_attempt(data):
            aid = str(uuid.uuid4()); db.attempts[aid] = {"id": aid, "started_at": _now(), "ended_at": None, "leg_control_id": None, **data}
            return db.attempts[aid]
        async def get_attempt(aid): return db.attempts.get(aid)
        async def update_attempt(aid, data): db.attempts[aid].update(data); return db.attempts[aid]
        async def attempts_for_call(cid): return [a for a in db.attempts.values() if a["call_id"] == cid]
        async def insert_callback(data):
            cbid = str(uuid.uuid4()); db.callbacks[cbid] = {"id": cbid, **data}; return db.callbacks[cbid]
        async def update_callback(cbid, data): db.callbacks[cbid].update(data); return db.callbacks[cbid]
        async def pending_callbacks_for_lead(lid):
            return [c for c in db.callbacks.values() if c["lead_id"] == lid and c["status"] in ("pending", "dialing")]
        async def add_dnc(entries):
            for e in entries: db.dnc[e["phone"]] = e
        async def is_dnc(phone): return phone in db.dnc
        async def get_system_config(): return dict(db.config)
        async def insert_call_event(data):
            if data["event_id"] in db.events:
                return False
            db.events.add(data["event_id"]); return True
        async def count_active_calls():
            return len([c for c in db.calls.values() if c["status"] in repo_module.IN_PROGRESS_CALL_STATUSES])
        async def count_active_calls_for_agent(agent_id):
            return len([c for c in db.calls.values() if c.get("agent_id") == agent_id
                        and c["status"] in repo_module.IN_PROGRESS_CALL_STATUSES])
        async def enabled_agents():
            return sorted([a for a in db.agents.values() if a["enabled"]], key=lambda a: (a["priority"], a["name"]))
        async def list_agents():
            return sorted(db.agents.values(), key=lambda a: (a["priority"], a["name"]))
        async def get_agent(aid): return db.agents.get(aid)
        async def update_agent(aid, data):
            db.agents[aid].update(data); return db.agents[aid]
        async def recent_finished_calls(limit=50):
            done = [c for c in db.calls.values() if c["status"] == "ENDED"]
            return sorted(done, key=lambda c: c.get("ended_at") or "", reverse=True)[:limit]
        async def save_dialer_state(state): db.dialer_state = dict(state)
        async def get_dialer_state(): return dict(getattr(db, "dialer_state", {}))
        async def audit(username, action, details=None):
            db.audits.append({"username": username, "action": action, "details": details or {}})

        for name, fn in list(locals().items()):
            if callable(fn) and name not in ("db", "self", "monkeypatch"):
                monkeypatch.setattr(repo_module, name, fn)


class FakeTelnyx:
    def __init__(self):
        self.dialed: list[dict] = []
        self.bridged: list[tuple] = []
        self.hung_up: list[str] = []
        self.gathers: list[str] = []
        self.streams: list[str] = []
        self.fail_dial_to: set[str] = set()

    configured = True

    async def dial(self, to, client_state, timeout_secs=30, amd_mode="disabled", time_limit_secs=3600, from_number=""):
        if to in self.fail_dial_to:
            from app.telephony.telnyx import TelnyxError
            raise TelnyxError(422, "invalid destination")
        leg = f"leg-{len(self.dialed) + 1}"
        self.dialed.append({"to": to, "state": client_state, "leg": leg, "amd": amd_mode, "from_number": from_number})
        return {"call_control_id": leg, "call_session_id": f"sess-{leg}"}

    async def gather_using_speak(self, ccid, text, client_state, timeout_ms=10000):
        self.gathers.append(text)

    async def bridge(self, a, b):
        self.bridged.append((a, b))

    async def hangup(self, ccid):
        self.hung_up.append(ccid)

    async def stop_stream(self, ccid):
        pass

    async def start_stream(self, ccid, url):
        self.streams.append(url)

    async def start_recording(self, ccid):
        pass
