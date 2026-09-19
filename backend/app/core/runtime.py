"""
Runtime settings — the values edited on the dashboard's Settings page.

Defaults live here; whatever an admin saves is stored in the system_config table
and overlaid on top. Every part of the system (dialer, voice bot, transfers,
scheduler) reads settings through get_runtime(), so a change on the Settings page
takes effect within a few seconds, without restarting anything.
"""

import time
from typing import Any

from loguru import logger

from app.db import repo

DEFAULTS: dict[str, Any] = {
    # ---- Identity used by the AI (spoken on every call) ----
    "company_name": "Your Company Name",
    "company_callback_number": "",  # a number people can call you back on (TCPA identification)
    "tpmo_org_count": "",  # "we represent [X] organizations..."
    "tpmo_product_count": "",  # "...which offer [X] products in your area"

    # ---- Calling hours, in EACH LEAD'S OWN local time ----
    "calling_start_local": "09:00",
    "calling_end_local": "20:00",
    "calling_days": [0, 1, 2, 3, 4, 5],  # 0=Mon ... 6=Sun
    "default_timezone": "America/New_York",  # used when a number's time zone can't be determined

    # ---- Dialer (super admin only) ----
    "max_concurrent_calls": 6,       # across all AI agents; each agent also has its own limit
    "amd_mode": "detect",            # detect | premium | disabled (voicemail detection on the lead's line)
    "ring_timeout_seconds": 30,
    "max_ai_call_minutes": 10,

    # ---- Retries: how long to wait and how many tries, per reason the call failed ----
    "retry_no_answer_hours": 4,
    "retry_no_answer_max": 3,
    "retry_busy_hours": 1,
    "retry_busy_max": 4,
    "retry_voicemail_hours": 24,
    "retry_voicemail_max": 2,
    "retry_hangup_hours": 48,
    "retry_hangup_max": 2,
    "max_retries": 6,                # overall cap per lead, whatever the reasons were
    "retry_time_of_day": "",         # blank = as soon as the wait is up; "17:00" = not before 5 PM local

    # ---- Transfers to closers (super admin only) ----
    "closer_ring_seconds": 20,
    "closer_require_accept": True,   # closer must press 1 before the lead is connected

    # ---- Safety brake: pause calling automatically when calls keep failing ----
    "brake_enabled": True,
    "brake_consecutive_failures": 5,   # this many failed calls in a row
    "brake_window_calls": 20,          # look at the last N finished calls...
    "brake_window_failure_percent": 60,  # ...and pause if this % or more failed
}

# Which settings only the super admin may change (the dialer, retries, transfers and the brake).
SUPER_ADMIN_KEYS = {
    "max_concurrent_calls", "amd_mode", "ring_timeout_seconds", "max_ai_call_minutes",
    "retry_no_answer_hours", "retry_no_answer_max", "retry_busy_hours", "retry_busy_max",
    "retry_voicemail_hours", "retry_voicemail_max", "retry_hangup_hours", "retry_hangup_max",
    "max_retries", "retry_time_of_day",
    "closer_ring_seconds", "closer_require_accept",
    "brake_enabled", "brake_consecutive_failures", "brake_window_calls", "brake_window_failure_percent",
}

# Retry rule per outcome group: (wait-hours setting, max-tries setting)
RETRY_RULES = {
    "no_answer": ("retry_no_answer_hours", "retry_no_answer_max"),
    "busy": ("retry_busy_hours", "retry_busy_max"),
    "voicemail": ("retry_voicemail_hours", "retry_voicemail_max"),
    "hangup": ("retry_hangup_hours", "retry_hangup_max"),
}

# Type coercion for values coming back from the Settings form.
_INT_KEYS = {"max_concurrent_calls", "max_retries", "ring_timeout_seconds", "max_ai_call_minutes",
             "closer_ring_seconds", "retry_no_answer_max", "retry_busy_max", "retry_voicemail_max",
             "retry_hangup_max", "brake_consecutive_failures", "brake_window_calls",
             "brake_window_failure_percent"}
_FLOAT_KEYS = {"retry_no_answer_hours", "retry_busy_hours", "retry_voicemail_hours", "retry_hangup_hours"}
_BOOL_KEYS = {"closer_require_accept", "brake_enabled"}

# Hard limits so a typo on the Settings page can't do something harmful.
_LIMITS = {
    "max_concurrent_calls": (1, 50),
    "max_retries": (0, 20),
    "ring_timeout_seconds": (10, 60),
    "max_ai_call_minutes": (2, 30),
    "closer_ring_seconds": (8, 60),
    "retry_no_answer_hours": (0.25, 336),
    "retry_busy_hours": (0.25, 336),
    "retry_voicemail_hours": (0.25, 336),
    "retry_hangup_hours": (0.25, 336),
    "retry_no_answer_max": (0, 10),
    "retry_busy_max": (0, 10),
    "retry_voicemail_max": (0, 10),
    "retry_hangup_max": (0, 10),
    "brake_consecutive_failures": (2, 50),
    "brake_window_calls": (5, 200),
    "brake_window_failure_percent": (10, 100),
}

_cache: dict[str, Any] = {"at": 0.0, "value": dict(DEFAULTS)}
_TTL = 5.0


def _coerce(key: str, value: Any) -> Any:
    if key in _BOOL_KEYS:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if key in _INT_KEYS:
        value = int(float(value))
    elif key in _FLOAT_KEYS:
        value = float(value)
    elif key == "calling_days":
        if isinstance(value, str):
            value = [int(x) for x in value.replace(" ", "").split(",") if x != ""]
        value = sorted({int(x) for x in value if 0 <= int(x) <= 6})
    elif isinstance(DEFAULTS.get(key), str):
        value = str(value).strip()
    if key in _LIMITS:
        lo, hi = _LIMITS[key]
        value = max(lo, min(hi, value))
    return value


def validate_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Keep only known keys, coerce types, clamp to safe limits. Raises ValueError on bad input."""
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        try:
            clean[key] = _coerce(key, value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid value for {key}: {value!r}")
    for key in ("calling_start_local", "calling_end_local", "retry_time_of_day"):
        if key in clean:
            if key == "retry_time_of_day" and not clean[key]:
                continue
            parts = clean[key].split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
                raise ValueError(f"{key} must look like 09:00")
            clean[key] = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if "amd_mode" in clean and clean["amd_mode"] not in {"detect", "premium", "disabled"}:
        raise ValueError("amd_mode must be detect, premium or disabled")
    if "default_timezone" in clean:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(clean["default_timezone"])
        except Exception:
            raise ValueError("default_timezone must be an IANA name like America/New_York")
    return clean


async def get_runtime(force: bool = False) -> dict[str, Any]:
    if not force and time.monotonic() - _cache["at"] < _TTL:
        return _cache["value"]
    merged = dict(DEFAULTS)
    try:
        saved = await repo.get_system_config()
        for key, value in saved.items():
            if key in DEFAULTS:
                try:
                    merged[key] = _coerce(key, value)
                except Exception:
                    logger.warning(f"Ignoring invalid saved setting {key}={value!r}")
    except Exception as e:
        logger.warning(f"Could not load runtime settings, using defaults: {e}")
    _cache.update(at=time.monotonic(), value=merged)
    return merged


async def save_runtime(updates: dict[str, Any]) -> dict[str, Any]:
    clean = validate_updates(updates)
    saved = await repo.get_system_config()
    saved.update(clean)
    await repo.save_system_config(saved)
    return await get_runtime(force=True)
