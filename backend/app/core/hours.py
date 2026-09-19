"""
Calling-hours rules, evaluated in the LEAD'S local time (not the server's).

Federal TCPA limit is 8am–9pm local time; some states are stricter (e.g. Florida
ends at 8pm). The default window on the Settings page is 9am–8pm, which sits
inside all of them.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Absolute outer bounds the Settings page can never go beyond.
FEDERAL_EARLIEST = "08:00"
FEDERAL_LATEST = "21:00"


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def effective_window(runtime: dict) -> tuple[str, str]:
    start = max(runtime["calling_start_local"], FEDERAL_EARLIEST)
    end = min(runtime["calling_end_local"], FEDERAL_LATEST)
    return start, end


def lead_zone(lead: dict, runtime: dict) -> ZoneInfo:
    for name in (lead.get("timezone"), runtime.get("default_timezone"), "America/New_York"):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    return ZoneInfo("America/New_York")


def is_callable_now(lead: dict, runtime: dict, now: datetime | None = None) -> bool:
    zone = lead_zone(lead, runtime)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if local.weekday() not in runtime["calling_days"]:
        return False
    start, end = effective_window(runtime)
    return start <= local.strftime("%H:%M") < end


def next_allowed_time(lead: dict, runtime: dict, not_before: datetime) -> datetime:
    """The earliest moment >= not_before that falls inside the lead's calling window (UTC)."""
    zone = lead_zone(lead, runtime)
    start, end = effective_window(runtime)
    sh, sm = _hm(start)
    local = not_before.astimezone(zone)
    days = runtime["calling_days"] or [0, 1, 2, 3, 4]
    for day_offset in range(0, 15):
        day = (local + timedelta(days=day_offset)).date()
        if day.weekday() not in days:
            continue
        window_start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=zone)
        eh, em = _hm(end)
        window_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=zone)
        candidate = max(window_start, local) if day_offset == 0 else window_start
        if candidate < window_end:
            return candidate.astimezone(timezone.utc)
    return not_before.astimezone(timezone.utc)


def local_time_description(lead: dict, runtime: dict, now: datetime | None = None) -> str:
    zone = lead_zone(lead, runtime)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    return f"{local.strftime('%A, %B %d, %Y at %I:%M %p').replace(' 0', ' ')} ({zone.key})"


def spoken_time(dt: datetime, lead: dict, runtime: dict) -> str:
    """e.g. 'Tuesday at 2:30 PM' / 'today at 4 PM' / 'tomorrow at 10 AM' in the lead's time zone."""
    zone = lead_zone(lead, runtime)
    local = dt.astimezone(zone)
    today = datetime.now(timezone.utc).astimezone(zone).date()
    minute = "" if local.minute == 0 else f":{local.minute:02d}"
    clock = f"{local.strftime('%I').lstrip('0')}{minute} {local.strftime('%p')}"
    if local.date() == today:
        return f"today at {clock}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow at {clock}"
    return f"{local.strftime('%A')} at {clock}"


def next_retry_time(lead: dict, runtime: dict, earliest: datetime) -> datetime:
    """When to try this lead again: after the wait has passed, not before the configured
    'try again after' time of day (if set), and always inside the lead's calling window."""
    hhmm = (runtime.get("retry_time_of_day") or "").strip()
    if hhmm:
        zone = lead_zone(lead, runtime)
        local = earliest.astimezone(zone)
        h, m = _hm(hhmm)
        target = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if local < target:
            earliest = target.astimezone(timezone.utc)
    return next_allowed_time(lead, runtime, earliest)
