"""
Google Sheets roster loader for name suggestions / auto-fill.

Expected columns (case-insensitive):
LAST_NAME, OTHER_NAME, NRIC, DOB, NATIONALITY, UNIQUE_ID, TEAM_NAME, TEAM_CODE

Auth:
- Uses Streamlit secrets with a Google service account dict under st.secrets["gcp_service_account"].

Requires:
- gspread
- google-auth
"""

from __future__ import annotations

from typing import Dict, List, Any
import re

import pandas as pd
import streamlit as st

try:
    import gspread
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    gspread = None  # type: ignore
    service_account = None  # type: ignore


_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def extract_sheet_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    m = _SHEET_ID_RE.search(s)
    if m:
        return m.group(1)
    return s



def get_service_account_email() -> str:
    """Return the service account email from secrets (if present) for sharing the sheet."""
    try:
        sa = dict(st.secrets.get("gcp_service_account", {}))
        return str(sa.get("client_email", "") or "")
    except Exception:
        return ""

@st.cache_resource(show_spinner=False)
def _get_gspread_client():
    if gspread is None or service_account is None:
        raise RuntimeError("Missing dependencies: install 'gspread' and 'google-auth' (add to requirements.txt).")

    if not (hasattr(st, "secrets") and "gcp_service_account" in st.secrets and st.secrets["gcp_service_account"]):
        raise RuntimeError("Missing st.secrets['gcp_service_account'] for Google Sheets access.")

    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)


@st.cache_data(show_spinner=False, ttl=300)
def load_roster(sheet_url_or_id: str, worksheet: str | None = None) -> List[Dict[str, Any]]:
    sheet_id = extract_sheet_id(sheet_url_or_id)
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    records = ws.get_all_records()
    out: List[Dict[str, Any]] = []
    for r in records:
        nr = {str(k).strip().upper(): v for k, v in (r or {}).items()}
        out.append(nr)
    return out


def parse_dob(value) -> Any:
    if value is None or value == "":
        return None
    try:
        dt = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def last4_from_nric(nric: str) -> str:
    s = (nric or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    if len(s) >= 4:
        return s[-4:]
    return s
