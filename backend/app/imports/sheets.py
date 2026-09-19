"""Google Sheets access: a private sheet shared with the service account, or a link-shared sheet."""

import asyncio
import json
import os
import re

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import BACKEND_DIR, settings
from app.imports.leads import read_table, rows_from_values

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetError(RuntimeError):
    pass


def extract_sheet_id(url_or_id: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id or "")
    return match.group(1) if match else (url_or_id or "").strip()


def _service_account_info() -> dict | None:
    raw = (settings.google_service_account_json or "").strip()
    candidates = []
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if raw:
        candidates += [raw, str(BACKEND_DIR / raw)]
    candidates += [str(BACKEND_DIR / "credentials.json"), str(BACKEND_DIR / "service_account.json")]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    info = json.load(fh)
                if info.get("type") == "service_account":
                    return info
            except Exception:
                continue
    return None


def service_account_email() -> str | None:
    info = _service_account_info()
    return (info or {}).get("client_email")


def _read_private(sheet_id: str) -> tuple[list[str], list[dict]]:
    info = _service_account_info()
    if not info:
        raise SheetError("no_service_account")
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    title = meta["sheets"][0]["properties"]["title"]
    values = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{title}'!A:ZZ").execute().get("values", [])
    return rows_from_values(values)


async def fetch_sheet(sheet_id: str) -> tuple[list[str], list[dict], str]:
    """Returns (headers, rows, mode). Tries the service account first, then a public CSV export."""
    private_error = None
    try:
        headers, data = await asyncio.to_thread(_read_private, sheet_id)
        return headers, data, "service_account"
    except SheetError:
        pass
    except HttpError as e:
        text = str(e)
        if "SERVICE_DISABLED" in text or "has not been used in project" in text:
            private_error = "The Google Sheets API is disabled for your service account's Google Cloud project. Enable it, then retry."
        elif e.resp.status in (401, 403, 404):
            email = service_account_email()
            private_error = (f"The sheet isn't shared with the service account. In Google Sheets click Share and add "
                             f"{email} as a Viewer." if email else "The sheet couldn't be opened.")
        else:
            private_error = f"Google Sheets error {e.resp.status}."
    except Exception as e:
        private_error = f"The service account couldn't open the sheet: {str(e)[:160]}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        for url in (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv",
                    f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"):
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content and not resp.content.lstrip().lower().startswith(b"<!doctype html"):
                    headers, data = read_table("sheet.csv", resp.content)
                    return headers, data, "public_link"
            except Exception:
                continue
    raise SheetError(private_error or "Couldn't read the sheet. Share it with the service account email shown on this page, "
                                      "or set it to 'Anyone with the link can view'.")
