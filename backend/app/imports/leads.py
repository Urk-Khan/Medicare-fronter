"""
Lead import from Excel/CSV files and Google Sheets.

Flow (same for both sources):
  1. preview  -> parse the rows, auto-detect which column is which, return a sample.
                 The parsed rows are held server-side for 30 minutes under a token.
  2. commit   -> the admin confirms/fixes the column mapping and how consent was
                 obtained; rows are cleaned (phone -> +1XXXXXXXXXX), de-duplicated,
                 checked against the do-not-call list and inserted.
"""

import io
import os
import secrets
import time
from datetime import datetime, timezone

import pandas as pd

from app.core.phones import normalize_phone, timezone_for_phone
from app.db import repo

FIELDS = {
    "first_name": ["first name", "firstname", "first", "fname", "given name"],
    "last_name": ["last name", "lastname", "last", "lname", "surname", "family name"],
    "full_name": ["full name", "name", "lead name", "contact name", "customer name"],
    "phone": ["phone", "phone number", "mobile", "mobile number", "cell", "cell phone", "telephone", "number", "contact number"],
    "email": ["email", "email address", "e-mail"],
    "zip_code": ["zip", "zip code", "zipcode", "postal code", "postcode"],
    "state": ["state", "st", "province", "region"],
    "source": ["source", "lead source", "campaign", "vendor"],
    "notes": ["notes", "note", "context", "comments", "description", "details"],
    "consent_at": ["consent date", "consent_at", "opt in date", "optin date", "opt-in date", "tcpa date", "consent timestamp"],
    "consent_source": ["consent source", "opt in source", "opt-in source", "consent url", "tcpa source", "optin url"],
}

_PREVIEWS: dict[str, dict] = {}
_PREVIEW_TTL = 30 * 60
MAX_ROWS = 50_000


def _norm_header(h: str) -> str:
    return str(h).strip().lower().replace("_", " ").replace("-", " ")


def detect_mapping(headers: list[str]) -> dict[str, str | None]:
    mapping: dict[str, str | None] = {}
    used: set[str] = set()
    normalized = {h: _norm_header(h) for h in headers}
    # Exact matches first, then "contains" matches, so "First Name" doesn't get grabbed by "name".
    for field, words in FIELDS.items():
        mapping[field] = next((h for h in headers if h not in used and normalized[h] in words), None)
        if mapping[field]:
            used.add(mapping[field])
    for field, words in FIELDS.items():
        if mapping[field]:
            continue
        mapping[field] = next(
            (h for h in headers if h not in used and any(w in normalized[h] for w in words if len(w) > 3)), None)
        if mapping[field]:
            used.add(mapping[field])
    return mapping


def read_table(filename: str, data: bytes) -> tuple[list[str], list[dict]]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding_errors="replace")
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
    else:
        raise ValueError("Only .csv, .xlsx and .xls files are supported.")
    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    if len(df) > MAX_ROWS:
        raise ValueError(f"File has {len(df)} rows; the maximum per import is {MAX_ROWS}.")
    return list(df.columns), df.to_dict("records")


def rows_from_values(values: list[list]) -> tuple[list[str], list[dict]]:
    if not values:
        return [], []
    headers = [str(h).strip() for h in values[0]]
    out = []
    for row in values[1:]:
        record = {h: (str(row[i]).strip() if i < len(row) else "") for i, h in enumerate(headers)}
        if any(record.values()):
            out.append(record)
    return headers, out


def _cleanup_previews() -> None:
    cutoff = time.time() - _PREVIEW_TTL
    for token in [t for t, p in _PREVIEWS.items() if p["at"] < cutoff]:
        _PREVIEWS.pop(token, None)


def create_preview(source: str, name: str, headers: list[str], data_rows: list[dict]) -> dict:
    _cleanup_previews()
    token = secrets.token_urlsafe(16)
    _PREVIEWS[token] = {"at": time.time(), "source": source, "name": name, "headers": headers, "rows": data_rows}
    mapping = detect_mapping(headers)
    return {
        "token": token,
        "source": source,
        "name": name,
        "headers": headers,
        "total_rows": len(data_rows),
        "detected_mapping": mapping,
        "sample_rows": data_rows[:10],
        "fields": list(FIELDS.keys()),
    }


def _parse_consent_date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        ts = pd.to_datetime(value, utc=True)
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().isoformat()
    except Exception:
        return None


def build_lead(row: dict, mapping: dict, consent_mode: str, attest_source: str, now_iso: str) -> tuple[dict | None, str]:
    def get(field: str) -> str:
        col = mapping.get(field)
        return str(row.get(col, "")).strip() if col else ""

    phone = normalize_phone(get("phone"))
    if not phone:
        return None, "invalid_phone"
    first, last = get("first_name"), get("last_name")
    if not first and not last and get("full_name"):
        parts = get("full_name").split()
        first, last = parts[0], " ".join(parts[1:])

    if consent_mode == "column":
        consent_at = _parse_consent_date(get("consent_at"))
        consent_source = get("consent_source") or "import (consent column)"
        if not consent_at:
            return None, "missing_consent"
    else:  # "attest": the admin confirmed every row in this file opted in
        consent_at = now_iso
        consent_source = attest_source

    mapped_cols = {c for c in mapping.values() if c}
    zip_code = "".join(ch for ch in get("zip_code") if ch.isdigit())[:5] or None
    return {
        "first_name": first.title() if first.isupper() or first.islower() else first,
        "last_name": last.title() if last.isupper() or last.islower() else last,
        "phone": phone,
        "phone_raw": get("phone"),
        "email": get("email") or None,
        "zip_code": zip_code.zfill(5) if zip_code else None,
        "state": get("state").upper()[:2] if len(get("state")) <= 3 else get("state"),
        "source": get("source"),
        "notes": get("notes"),
        "custom_fields": {k: v for k, v in row.items() if k not in mapped_cols and str(v).strip()},
        "timezone": timezone_for_phone(phone),
        "consent_at": consent_at,
        "consent_source": consent_source,
        "status": "new",
    }, "ok"


async def commit_preview(token: str, mapping: dict, consent_mode: str, attest: bool, attest_source: str,
                         username: str | None) -> dict:
    preview = _PREVIEWS.get(token)
    if not preview:
        raise ValueError("This preview expired. Please upload the file again.")
    if not mapping.get("phone"):
        raise ValueError("Choose which column contains the phone number.")
    if consent_mode not in ("column", "attest"):
        raise ValueError("Choose how consent was obtained.")
    if consent_mode == "column" and not mapping.get("consent_at"):
        raise ValueError("Choose the column that contains the consent date.")
    if consent_mode == "attest":
        if not attest:
            raise ValueError("You must confirm that every lead in this list gave permission to be contacted.")
        if len((attest_source or "").strip()) < 3:
            raise ValueError("Describe where the consent came from (for example: 'Web form on example.com, TCPA opt-in').")

    now_iso = datetime.now(timezone.utc).isoformat()
    built: dict[str, dict] = {}
    invalid = missing_consent = duplicates_in_file = 0
    for row in preview["rows"]:
        lead, status = build_lead(row, mapping, consent_mode, (attest_source or "").strip(), now_iso)
        if status == "invalid_phone":
            invalid += 1
            continue
        if status == "missing_consent":
            missing_consent += 1
            continue
        if lead["phone"] in built:
            duplicates_in_file += 1
            continue
        built[lead["phone"]] = lead

    phones = list(built.keys())
    existing = {r["phone"] for r in await repo.get_leads_by_phones(phones)}
    dnc = await repo.dnc_among(phones)
    to_insert = [lead for phone, lead in built.items() if phone not in existing and phone not in dnc]
    imported = await repo.insert_leads(to_insert) if to_insert else 0

    result = {
        "total_rows": len(preview["rows"]),
        "imported": imported,
        "duplicates": duplicates_in_file + len(existing),
        "invalid_phone": invalid,
        "missing_consent": missing_consent,
        "dnc_skipped": len([p for p in phones if p in dnc and p not in existing]),
    }
    await repo.insert_import_run({
        "source": preview["source"],
        "name": preview["name"],
        "total_rows": result["total_rows"],
        "imported": imported,
        "duplicates": result["duplicates"],
        "invalid": invalid + missing_consent,
        "dnc_skipped": result["dnc_skipped"],
        "created_by": username,
    })
    _PREVIEWS.pop(token, None)
    return result
