"""Thin async client for the Telnyx Call Control API v2 (everything this app uses, nothing more)."""

import base64
import json
import uuid
from typing import Any

import httpx
from loguru import logger

from app.config import settings

BASE = "https://api.telnyx.com/v2"


class TelnyxError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Telnyx API error {status}: {body[:300]}")
        self.status = status
        self.body = body

    @property
    def call_gone(self) -> bool:
        """422 'call has already ended' / 404 — the leg no longer exists."""
        return self.status in (404, 410) or (self.status == 422 and "ended" in self.body.lower())


def encode_state(data: dict) -> str:
    return base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def decode_state(value: str | None) -> dict:
    if not value:
        return {}
    try:
        return json.loads(base64.b64decode(value).decode())
    except Exception:
        return {}


class TelnyxClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=BASE, timeout=15.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(settings.telnyx_api_key and settings.telnyx_connection_id and settings.telnyx_from_number)

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {settings.telnyx_api_key}", "Content-Type": "application/json"}
        resp = await self._http().request(method, path, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise TelnyxError(resp.status_code, resp.text)
        if not resp.content:
            return {}
        return resp.json().get("data", {}) or {}

    async def command(self, call_control_id: str, action: str, payload: dict | None = None) -> dict[str, Any]:
        body = {"command_id": str(uuid.uuid4()), **(payload or {})}
        return await self._request("POST", f"/calls/{call_control_id}/actions/{action}", body)

    # ---- outbound legs -------------------------------------------------------

    async def dial(
        self,
        to: str,
        client_state: dict,
        timeout_secs: int = 30,
        amd_mode: str = "disabled",
        time_limit_secs: int = 3600,
        from_number: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "connection_id": settings.telnyx_connection_id,
            "to": to,
            "from": (from_number or "").strip() or settings.telnyx_from_number,
            "timeout_secs": timeout_secs,
            "time_limit_secs": time_limit_secs,
            "client_state": encode_state(client_state),
        }
        if amd_mode in ("detect", "premium"):
            body["answering_machine_detection"] = amd_mode
            if amd_mode == "detect":
                # Keep the delay before the AI speaks short.
                body["answering_machine_detection_config"] = {"total_analysis_time_millis": 3500}
        return await self._request("POST", "/calls", body)

    # ---- in-call commands ----------------------------------------------------

    async def start_stream(self, call_control_id: str, stream_url: str) -> dict:
        return await self.command(call_control_id, "streaming_start", {
            "stream_url": stream_url,
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
        })

    async def stop_stream(self, call_control_id: str) -> None:
        try:
            await self.command(call_control_id, "streaming_stop", {})
        except TelnyxError as e:
            logger.debug(f"streaming_stop ignored: {e}")

    async def start_recording(self, call_control_id: str) -> None:
        try:
            await self.command(call_control_id, "record_start", {"format": "mp3", "channels": "dual", "play_beep": False})
        except TelnyxError as e:
            logger.warning(f"record_start failed for {call_control_id}: {e}")

    async def bridge(self, call_control_id: str, other_call_control_id: str) -> dict:
        return await self.command(call_control_id, "bridge", {"call_control_id": other_call_control_id})

    async def gather_using_speak(self, call_control_id: str, text: str, client_state: dict, timeout_ms: int = 10000) -> dict:
        return await self.command(call_control_id, "gather_using_speak", {
            "payload": text,
            "voice": "female",
            "language": "en-US",
            "minimum_digits": 1,
            "maximum_digits": 1,
            "valid_digits": "0123456789*#",
            "timeout_millis": timeout_ms,
            "client_state": encode_state(client_state),
        })

    async def hangup(self, call_control_id: str) -> None:
        try:
            await self.command(call_control_id, "hangup", {})
        except TelnyxError as e:
            if not e.call_gone:
                logger.warning(f"hangup failed for {call_control_id}: {e}")
        except Exception as e:
            logger.warning(f"hangup failed for {call_control_id}: {e}")

    async def recording_url(self, recording_id: str) -> str | None:
        data = await self._request("GET", f"/recordings/{recording_id}")
        urls = data.get("download_urls") or {}
        return urls.get("mp3") or urls.get("wav")

    async def update_webhook_url(self, webhook_url: str) -> None:
        """Point the Call Control Application's webhooks at this backend (used by start.py)."""
        await self._request("PATCH", f"/call_control_applications/{settings.telnyx_connection_id}", {
            "webhook_event_url": webhook_url,
            "webhook_api_version": "2",
        })


TELNYX = TelnyxClient()
