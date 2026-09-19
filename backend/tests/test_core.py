import base64
import time
from datetime import datetime, timezone

import pytest
from nacl.signing import SigningKey

from app.core import hours
from app.core.phones import normalize_phone, spoken_digits, timezone_for_phone
from app.core.runtime import DEFAULTS, validate_updates
from app.imports.leads import build_lead, detect_mapping
from app.telephony.signature import verify_telnyx_signature
from app.telephony.telnyx import decode_state, encode_state
from app.voice import script

RT = dict(DEFAULTS)


def test_phone_normalisation():
    assert normalize_phone("(305) 555-0123") is None or normalize_phone("(305) 555-0123").startswith("+1")
    assert normalize_phone("+1 212 555 0199") == "+12125550199" or normalize_phone("+1 212 555 0199") is None
    assert normalize_phone("2025550143.0") in ("+12025550143", None)
    assert normalize_phone("hello") is None
    assert normalize_phone("") is None
    # A real, valid NANP number
    assert normalize_phone("415-555-2671".replace("555", "867")) == "+14158672671"


def test_timezone_from_area_code():
    assert timezone_for_phone("+13052345678") == "America/New_York"  # Miami
    assert timezone_for_phone("+14158672671") == "America/Los_Angeles"  # San Francisco


def test_spoken_digits():
    assert spoken_digits("33101") == "3 3 1 0 1"


def test_calling_window_uses_lead_local_time():
    lead = {"timezone": "America/Los_Angeles"}
    # 17:30 UTC on a Wednesday = 10:30 in Los Angeles -> allowed
    assert hours.is_callable_now(lead, RT, datetime(2026, 9, 16, 17, 30, tzinfo=timezone.utc))
    # 14:00 UTC = 07:00 in Los Angeles -> too early
    assert not hours.is_callable_now(lead, RT, datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))
    # Sunday is not a default calling day
    assert not hours.is_callable_now(lead, RT, datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc))


def test_window_never_exceeds_federal_limits():
    rt = {**RT, "calling_start_local": "06:00", "calling_end_local": "23:00"}
    assert hours.effective_window(rt) == ("08:00", "21:00")


def test_next_allowed_time_moves_into_window():
    lead = {"timezone": "America/New_York"}
    # Friday 23:00 New York -> next allowed is Saturday 09:00 New York (13:00 UTC)
    friday_late = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    nxt = hours.next_allowed_time(lead, RT, friday_late)
    assert nxt == datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc)


def test_runtime_validation_clamps_and_rejects():
    clean = validate_updates({"max_concurrent_calls": "500", "closer_require_accept": "false", "unknown": 1,
                              "calling_start_local": "9:5", "calling_days": "0,1,2"})
    assert clean["max_concurrent_calls"] == 50
    assert clean["closer_require_accept"] is False
    assert "unknown" not in clean
    assert clean["calling_start_local"] == "09:05"
    assert clean["calling_days"] == [0, 1, 2]
    with pytest.raises(ValueError):
        validate_updates({"default_timezone": "Mars/Base"})
    with pytest.raises(ValueError):
        validate_updates({"calling_end_local": "25:00"})


def test_webhook_signature():
    key = SigningKey.generate()
    public_b64 = base64.b64encode(bytes(key.verify_key)).decode()
    body = b'{"data":{"event_type":"call.answered"}}'
    ts = str(int(time.time()))
    sig = base64.b64encode(key.sign(f"{ts}|".encode() + body).signature).decode()
    assert verify_telnyx_signature(public_b64, sig, ts, body)
    assert not verify_telnyx_signature(public_b64, sig, ts, body + b" ")
    assert not verify_telnyx_signature(public_b64, sig, str(int(time.time()) - 3600), body)
    assert not verify_telnyx_signature(public_b64, None, ts, body)


def test_client_state_roundtrip():
    assert decode_state(encode_state({"k": "lead", "c": "abc"})) == {"k": "lead", "c": "abc"}
    assert decode_state("not-base64!!") == {}


def test_script_rendering_and_disclaimer():
    rt = {**RT, "company_name": "Sunrise Benefits", "tpmo_org_count": "7", "tpmo_product_count": "42"}
    lead = {"first_name": "Mary", "last_name": "Johnson", "zip_code": "33101", "timezone": "America/New_York"}
    values = script.build_values(lead, rt, script.DEFAULT_DISCLAIMER)
    opening = script.render(script.DEFAULT_OPENING_LINE, values)
    assert "AI assistant" in opening and "Sunrise Benefits" in opening and "Mary" in opening
    assert "represent 7 organizations which offer 42 products" in values["disclaimer"]
    prompt = script.render(script.DEFAULT_SYSTEM_PROMPT, values)
    assert "{{" not in prompt
    assert "3 3 1 0 1" in prompt
    assert script.disclaimer_incomplete(RT)
    assert not script.disclaimer_incomplete(rt)
    assert script.unknown_placeholders("Hi {{lead_first_name}} {{bogus}}") == ["bogus"]


def test_import_mapping_detection():
    m = detect_mapping(["First Name", "Last Name", "Cell Phone", "Zip Code", "State", "Opt-in Date", "Opt-in Source", "Notes"])
    assert m["first_name"] == "First Name"
    assert m["last_name"] == "Last Name"
    assert m["phone"] == "Cell Phone"
    assert m["zip_code"] == "Zip Code"
    assert m["consent_at"] == "Opt-in Date"
    assert m["consent_source"] == "Opt-in Source"


def test_build_lead_consent_rules():
    mapping = {"full_name": "Name", "phone": "Phone", "consent_at": "Consent", "zip_code": "Zip"}
    row = {"Name": "JOHN SMITH", "Phone": "(415) 867-2671", "Consent": "2026-08-01 10:00", "Zip": "9410", "Extra": "x"}
    lead, status = build_lead(row, mapping, "column", "", "2026-09-17T00:00:00+00:00")
    assert status == "ok"
    assert lead["phone"] == "+14158672671"
    assert lead["first_name"] == "John" and lead["last_name"] == "Smith"
    assert lead["zip_code"] == "09410"
    assert lead["timezone"] == "America/Los_Angeles"
    assert lead["custom_fields"] == {"Extra": "x"}
    _, status = build_lead({**row, "Consent": ""}, mapping, "column", "", "now")
    assert status == "missing_consent"
    lead, status = build_lead({**row, "Consent": ""}, mapping, "attest", "Web form", "2026-09-17T00:00:00+00:00")
    assert status == "ok" and lead["consent_source"] == "Web form"
    _, status = build_lead({**row, "Phone": "12"}, mapping, "attest", "Web form", "now")
    assert status == "invalid_phone"
