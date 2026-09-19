"""Phone number normalisation and time-zone lookup (US-focused, E.164 everywhere)."""

import re

import phonenumbers
from phonenumbers import timezone as pn_timezone


def normalize_phone(raw: object, default_region: str = "US") -> str | None:
    """Turn '(305) 555-0123', '3055550123', '+1 305 555 0123' into '+13055550123'.

    Returns None if it isn't a valid, dialable number.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    # Spreadsheets often turn phone numbers into floats: 3055550123.0
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    try:
        parsed = phonenumbers.parse(text, None if text.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def timezone_for_phone(e164: str) -> str | None:
    """Best-guess IANA time zone for a number from its area code, or None if ambiguous/unknown."""
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return None
    zones = [z for z in pn_timezone.time_zones_for_number(parsed) if z and z != "Etc/Unknown"]
    return zones[0] if len(zones) == 1 else None


def pretty_phone(e164: str) -> str:
    try:
        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    except Exception:
        return e164


def spoken_digits(value: str) -> str:
    """'33101' -> '3 3 1 0 1' so TTS reads ZIPs digit by digit."""
    return " ".join(ch for ch in str(value) if ch.isdigit())
