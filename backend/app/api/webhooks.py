"""Telnyx webhook endpoint (signature-verified) and the media WebSocket the AI pipeline runs on."""

import asyncio
import hmac
import json

from fastapi import APIRouter, HTTPException, Request, WebSocket
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket

from app.calls import lifecycle
from app.config import settings
from app.core.runtime import get_runtime
from app.core.tasks import spawn
from app.db import repo
from app.telephony.signature import verify_telnyx_signature
from app.telephony.telnyx import decode_state
from app.voice import bot

router = APIRouter()


@router.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request):
    body = await request.body()
    if settings.telnyx_webhook_public_key:
        ok = verify_telnyx_signature(
            settings.telnyx_webhook_public_key,
            request.headers.get("telnyx-signature-ed25519"),
            request.headers.get("telnyx-timestamp"),
            body,
        )
        if not ok:
            logger.warning("Rejected Telnyx webhook with an invalid signature")
            raise HTTPException(401, "invalid signature")
    elif settings.is_production:
        raise HTTPException(503, "TELNYX_WEBHOOK_PUBLIC_KEY must be set in production")

    try:
        event = json.loads(body).get("data") or {}
    except Exception:
        raise HTTPException(400, "invalid json")

    # Answer Telnyx immediately; process in the background so slow DB/API calls never
    # cause Telnyx to time out and retry. Events for the same call leg are processed
    # strictly in arrival order (answered before hangup, etc.).
    key = _order_key(event)
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    spawn(_process, event, key, lock, label="telnyx webhook")
    return {"ok": True}


_LOCKS: dict[str, asyncio.Lock] = {}


def _order_key(event: dict) -> str:
    payload = event.get("payload") or {}
    state = decode_state(payload.get("client_state"))
    if state.get("c"):
        return f"{state.get('k')}:{state.get('c')}:{state.get('a', '')}"
    return payload.get("call_control_id") or "misc"


async def _process(event: dict, key: str, lock: asyncio.Lock) -> None:
    async with lock:
        try:
            await lifecycle.handle_event(event)
        except Exception as e:
            logger.exception(f"webhook {event.get('event_type')} failed: {e}")
    if event.get("event_type") == "call.hangup" and not lock.locked():
        _LOCKS.pop(key, None)


@router.websocket("/ws/telnyx/media")
async def telnyx_media(websocket: WebSocket):
    call_id = websocket.query_params.get("call_id") or ""
    token = websocket.query_params.get("token") or ""
    await websocket.accept()

    call = await repo.get_call(call_id) if call_id else None
    if not call or not call.get("stream_token") or not hmac.compare_digest(call["stream_token"], token):
        logger.warning(f"media websocket rejected for call_id={call_id!r}")
        await websocket.close(code=1008)
        return
    if call.get("status") not in ("AI_CONVERSATION",):
        logger.warning(f"media websocket for call {call_id[-8:]} in status {call.get('status')} — closing")
        await websocket.close(code=1008)
        return

    try:
        transport_type, call_data = await parse_telephony_websocket(websocket)
    except Exception as e:
        logger.error(f"media handshake failed for call {call_id[-8:]}: {e}")
        return
    if transport_type != "telnyx" or not call_data.get("stream_id"):
        logger.error(f"unexpected media handshake for call {call_id[-8:]}: {transport_type}")
        await websocket.close(code=1008)
        return

    lead = await repo.get_lead(call["lead_id"]) if call.get("lead_id") else {}
    runtime = await get_runtime()
    agent = (await repo.get_agent(call["agent_id"]) if call.get("agent_id") else None) or {}
    if not agent:
        # A call placed before any agent existed, or the agent was deleted mid-call.
        agents = await repo.enabled_agents()
        agent = agents[0] if agents else {"name": runtime.get("company_name") and "Ava" or "Ava"}
    await bot.run_call(websocket, call_data, call, lead or {}, runtime, agent)
