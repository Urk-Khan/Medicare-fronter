"""
Outbound dialer + background maintenance.

Dialer (only while "Start Calling" is on):
  every second, if we're under the overall concurrent-call limit, pick the next AI agent that
  has spare capacity, then the next lead that is allowed to be called right now, and dial it.
  A lead is callable only if it has recorded consent, isn't on the do-not-call list, is due
  (new / retry time reached / callback time reached), and it's inside calling hours in the
  lead's own time zone. Leads are claimed atomically, so two agents can never call the same
  person at the same time.

Safety brake:
  if calls keep failing (a run of failures, or too high a failure rate over the last N calls)
  the dialer pauses itself and records why, instead of burning through the lead list.

Maintenance (always running):
  * calls stuck ringing/connecting (webhook never arrived) are closed out
  * AI conversations that outlived their pipeline are closed out
  * closers stuck as busy are freed (safety net; not configurable)
"""

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.calls import lifecycle, outcomes, session as sessions
from app.core import hours
from app.core.runtime import get_runtime
from app.core.tasks import spawn
from app.db import repo
from app.telephony.telnyx import TELNYX, TelnyxError

# Safety net for a closer left marked busy because a hang-up webhook never arrived.
CLOSER_SAFETY_MINUTES = 45


class DialError(RuntimeError):
    pass


class NoAgentAvailable(DialError):
    pass


class Dialer:
    def __init__(self) -> None:
        self.running = False
        self.started_by: str | None = None
        self.started_at: datetime | None = None
        self.last_error: str | None = None
        self.paused_reason: str | None = None   # set by the safety brake
        self.paused_at: datetime | None = None
        self._placing: dict[int, str] = {}      # lead id -> agent id currently being dialed
        self._loop_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None

    # ---- lifecycle -----------------------------------------------------------

    def boot(self) -> None:
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._dial_loop())
        spawn(self._restore_state, label="restore dialer state")

    async def _restore_state(self) -> None:
        state = await repo.get_dialer_state()
        if state.get("paused_reason") and not self.running:
            self.paused_reason = state["paused_reason"]

    async def shutdown(self) -> None:
        self.running = False
        for task in (self._loop_task, self._maintenance_task):
            if task:
                task.cancel()
        # So a later boot() (e.g. the app restarting inside the same process) starts fresh loops.
        self._loop_task = None
        self._maintenance_task = None

    def start(self, username: str | None = None) -> None:
        self.running = True
        self.started_by = username
        self.started_at = datetime.now(timezone.utc)
        self.last_error = None
        self.paused_reason = None
        self.paused_at = None
        spawn(repo.save_dialer_state, {"paused_reason": None, "started_by": username,
                                       "started_at": self.started_at.isoformat()},
              label="save dialer state")
        logger.info(f"DIALER STARTED by {username}")

    def stop(self, username: str | None = None) -> None:
        self.running = False
        logger.info(f"DIALER STOPPED by {username}")

    # ---- dialing -------------------------------------------------------------

    async def _dial_loop(self) -> None:
        while True:
            try:
                if self.running:
                    placed = await self._tick()
                    await asyncio.sleep(0.3 if placed else 2.0)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)[:300]
                logger.exception(f"dialer tick failed: {e}")
                await asyncio.sleep(5.0)

    async def _tick(self) -> bool:
        if not TELNYX.configured:
            self.last_error = "Telnyx is not configured (TELNYX_API_KEY / TELNYX_CONNECTION_ID / TELNYX_FROM_NUMBER)"
            return False
        runtime = await get_runtime()
        active = await repo.count_active_calls()
        if active + len(self._placing) >= runtime["max_concurrent_calls"]:
            return False
        agent = await self.pick_agent()
        if not agent:
            self.last_error = None
            return False
        now = datetime.now(timezone.utc)
        candidates = await repo.dialable_lead_candidates(now.isoformat(), limit=60)
        if not candidates:
            return False
        dnc = await repo.dnc_among([c["phone"] for c in candidates])
        for lead in candidates:
            if lead["id"] in self._placing:
                continue
            if lead["phone"] in dnc:
                await repo.update_lead(lead["id"], {"status": "do_not_call", "next_attempt_at": None})
                continue
            if not hours.is_callable_now(lead, runtime, now):
                continue
            claimed = await repo.claim_lead(lead["id"], lead["status"])
            if not claimed:
                continue  # another agent got this lead a moment ago
            spawn(self._place_safely, claimed, lead["status"], agent, label=f"dial lead {claimed['id']}")
            return True
        return False

    async def pick_agent(self) -> dict | None:
        """The first enabled AI agent with spare capacity (lowest priority number first)."""
        agents = await repo.enabled_agents()
        for agent in agents:
            placing = len([a for a in self._placing.values() if a == agent["id"]])
            in_progress = await repo.count_active_calls_for_agent(agent["id"])
            if in_progress + placing < max(1, int(agent.get("max_concurrent_calls") or 1)):
                return agent
        if not agents:
            self.last_error = "No AI agent is switched on — add or enable one on the Agents page."
        return None

    async def _place_safely(self, lead: dict, previous_status: str, agent: dict) -> None:
        try:
            await self.place_call(lead, previous_status=previous_status, agent=agent)
        except Exception as e:
            logger.warning(f"could not place call to lead {lead['id']}: {e}")

    async def place_call(self, lead: dict, previous_status: str, agent: dict, manual: bool = False) -> dict:
        """Create the call row and dial. The lead must already be claimed (status 'calling')."""
        lead_id = lead["id"]
        self._placing[lead_id] = agent["id"]
        runtime = await get_runtime()
        try:
            if await repo.is_dnc(lead["phone"]):
                await repo.update_lead(lead_id, {"status": "do_not_call", "next_attempt_at": None})
                raise DialError("This number is on the do-not-call list.")
            is_callback = previous_status == "callback_scheduled"
            callback_rows = await repo.pending_callbacks_for_lead(lead_id) if is_callback else []
            call = await repo.insert_call({
                "lead_id": lead_id,
                "lead_name": f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
                "phone": lead["phone"],
                "lead_zip": lead.get("zip_code"),
                "lead_state": lead.get("state"),
                "lead_source": lead.get("source") or "",
                "agent_id": agent["id"],
                "agent_name": agent.get("name") or "",
                "status": "DIALING",
                "retry_number": lead.get("retry_count") or 0,
                "is_callback": is_callback,
                "callback_id": callback_rows[0]["id"] if callback_rows else None,
                "qualification": lead.get("qualification") or {},
            })
            await repo.update_lead(lead_id, {"current_call_id": call["id"], "last_call_at": call["started_at"]})
            for cb in callback_rows:
                await repo.update_callback(cb["id"], {"status": "dialing"})
            try:
                leg = await TELNYX.dial(
                    to=lead["phone"],
                    client_state={"k": "lead", "c": call["id"]},
                    timeout_secs=runtime["ring_timeout_seconds"],
                    amd_mode=runtime["amd_mode"],
                    time_limit_secs=4 * 3600,
                    from_number=agent.get("from_number") or "",
                )
            except TelnyxError as e:
                await repo.update_call(call["id"], {"outcome": "failed", "hangup_cause": f"dial_error_{e.status}"})
                await lifecycle.finalize_call(call["id"], f"dial_error_{e.status}")
                raise DialError(f"Telnyx refused the call: {e.body[:200]}")
            call = await repo.update_call(call["id"], {
                "telnyx_call_control_id": leg.get("call_control_id"),
                "telnyx_call_session_id": leg.get("call_session_id"),
            }) or call
            spawn(repo.update_agent, agent["id"], {"total_calls": int(agent.get("total_calls") or 0) + 1},
                  label="agent call count")
            logger.info(f"{'MANUAL ' if manual else ''}DIAL lead={lead_id} {lead['phone']} "
                        f"agent={agent.get('name')} call={call['id'][-8:]}")
            return call
        except DialError:
            raise
        except Exception:
            # Put the lead back where it was so it isn't stuck in 'calling'.
            fresh = await repo.get_lead(lead_id)
            if fresh and fresh.get("status") == "calling" and not fresh.get("current_call_id"):
                await repo.update_lead(lead_id, {"status": previous_status})
            raise
        finally:
            self._placing.pop(lead_id, None)

    async def call_now(self, lead_id: int) -> dict:
        """'Call Now' button. Same safety checks as the dialer, but skips the queue order."""
        lead = await repo.get_lead(lead_id)
        if not lead:
            raise DialError("Lead not found.")
        if not TELNYX.configured:
            raise DialError("Telnyx is not configured in backend/.env.")
        if not lead.get("consent_at"):
            raise DialError("This lead has no recorded consent to be contacted, so it can't be called.")
        if lead["status"] in ("do_not_call", "wrong_number") or await repo.is_dnc(lead["phone"]):
            raise DialError("This number is on the do-not-call list.")
        if lead.get("current_call_id") or lead["status"] == "calling":
            raise DialError("This lead is already on a call.")
        runtime = await get_runtime()
        if not hours.is_callable_now(lead, runtime):
            local = hours.local_time_description(lead, runtime)
            raise DialError(f"Outside calling hours for this lead (their local time is {local}).")
        active = await repo.count_active_calls()
        if active >= runtime["max_concurrent_calls"]:
            raise DialError("The maximum number of simultaneous calls is already in progress.")
        agent = await self.pick_agent()
        if not agent:
            raise NoAgentAvailable("Every AI agent is at its call limit (or none is switched on). Try again in a moment.")
        claimed = await repo.claim_lead(lead_id, lead["status"])
        if not claimed:
            raise DialError("This lead was just picked up by the dialer.")
        return await self.place_call(claimed, previous_status=lead["status"], agent=agent, manual=True)

    # ---- safety brake --------------------------------------------------------

    async def check_brake(self) -> None:
        """Pause calling when calls keep failing. Only looks at calls made since 'Start calling'."""
        if not self.running:
            return
        runtime = await get_runtime()
        if not runtime["brake_enabled"]:
            return
        window = int(runtime["brake_window_calls"])
        streak_limit = int(runtime["brake_consecutive_failures"])
        rows = await repo.recent_finished_calls(limit=max(window, streak_limit) + 5)
        if self.started_at:
            since = self.started_at.isoformat()
            rows = [r for r in rows if (r.get("ended_at") or r.get("started_at") or "") >= since]
        if not rows:
            return

        def failed(row: dict) -> bool:
            return (row.get("outcome") or "") in outcomes.FAILURE_OUTCOMES

        streak = 0
        for row in rows:
            if failed(row):
                streak += 1
            else:
                break
        if streak >= streak_limit:
            await self._trip(f"{streak} calls in a row didn't reach anybody. Calling paused so you can check "
                             f"your Telnyx number and balance.")
            return
        recent = rows[:window]
        if len(recent) >= window:
            fails = len([r for r in recent if failed(r)])
            percent = round(100 * fails / len(recent))
            if percent >= int(runtime["brake_window_failure_percent"]):
                await self._trip(f"{fails} of the last {len(recent)} calls failed ({percent}%). Calling paused "
                                 f"so you can check your numbers and lead list.")

    async def _trip(self, reason: str) -> None:
        self.running = False
        self.paused_reason = reason
        self.paused_at = datetime.now(timezone.utc)
        logger.warning(f"SAFETY BRAKE: {reason}")
        await repo.save_dialer_state({"paused_reason": reason, "paused_at": self.paused_at.isoformat()})
        await repo.audit("system", "dialer_paused_by_safety_brake", {"reason": reason})

    # ---- maintenance ---------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await self.sweep()
                await self.check_brake()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"maintenance sweep failed: {e}")

    async def sweep(self) -> None:
        runtime = await get_runtime()
        now = datetime.now(timezone.utc)

        # 1. Calls that never got past dialing (lost webhook, tunnel down, ...).
        ring_cutoff = (now - timedelta(seconds=runtime["ring_timeout_seconds"] + 90)).isoformat()
        for call in await repo.stale_calls(["DIALING", "RINGING", "ANSWERED"], ring_cutoff):
            logger.warning(f"closing stale {call['status']} call {call['id'][-8:]}")
            if call.get("telnyx_call_control_id"):
                await TELNYX.hangup(call["telnyx_call_control_id"])
            await lifecycle.finalize_call(call["id"], "stale_no_webhook")

        # 2. AI conversations with no live pipeline for a long time.
        ai_cutoff = (now - timedelta(minutes=runtime["max_ai_call_minutes"] + 5)).isoformat()
        live = set(sessions.active_ids())
        for call in await repo.stale_calls(["AI_CONVERSATION", "TRANSFERRING"], ai_cutoff):
            if call["id"] in live:
                continue
            logger.warning(f"closing orphaned AI call {call['id'][-8:]}")
            if call.get("telnyx_call_control_id"):
                await TELNYX.hangup(call["telnyx_call_control_id"])
            await lifecycle.finalize_call(call["id"], "orphaned_ai_call")

        # 3. Closers stuck as busy (safety net for a missing hang-up webhook).
        closer_cutoff = (now - timedelta(minutes=CLOSER_SAFETY_MINUTES)).isoformat()
        for closer in await repo.stuck_closers(closer_cutoff):
            call = await repo.get_call(closer["current_call_id"]) if closer.get("current_call_id") else None
            if call and call.get("status") in ("TRANSFERRED",) and not call.get("ended_at"):
                continue  # genuinely still talking to the lead
            logger.warning(f"freeing stuck closer {closer['name']}")
            await repo.release_closer(closer["id"])


dialer = Dialer()
