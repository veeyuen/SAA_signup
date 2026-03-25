"""
Google Sheets roster loader for name suggestions / auto-fill.

Expected columns (case-insensitive; spaces treated like underscores):
GENDER, FIRST_NAME, LAST_NAME, OTHER_NAME, NRIC, DOB, NATIONALITY, UNIQUE_ID, TEAM_NAME, TEAM_CODE

Auth:
- Uses Streamlit secrets with a Google service account dict under st.secrets["gcp_service_account"].

Requires:
- gspread
- google-auth
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional
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

EXPECTED_KEYS = {
    "GENDER",
    "FIRST_NAME",
    "LAST_NAME",
    "OTHER_NAME",
    "NRIC",
    "DOB",
    "NATIONALITY",
    "UNIQUE_ID",
    "TEAM_NAME",
    "TEAM_CODE",
}

def extract_sheet_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    m = _SHEET_ID_RE.search(s)
    if m:
        return m.group(1)
    return s

def normalize_key(k: str) -> str:
    k = (k or "").strip().upper()
    k = re.sub(r"\s+", "_", k)
    return k

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

def _records_from_values(values: List[List[Any]]) -> List[Dict[str, Any]]:
    """Build records from raw sheet values by locating the header row."""
    if not values:
        return []
    # Find header row: must contain LAST_NAME or at least 2 expected keys
    header_idx: Optional[int] = None
    for i, row in enumerate(values[:30]):  # search top 30 rows
        keys = {normalize_key(str(x)) for x in row if x is not None and str(x).strip() != ""}
        if "LAST_NAME" in keys or len(keys.intersection(EXPECTED_KEYS)) >= 2:
            header_idx = i
            break
    if header_idx is None:
        return []

    header = [normalize_key(str(x)) for x in values[header_idx]]
    out: List[Dict[str, Any]] = []
    for row in values[header_idx + 1 :]:
        if not any(str(x).strip() for x in row if x is not None):
            continue
        rec = {}
        for j, key in enumerate(header):
            if not key:
                continue
            rec[key] = row[j] if j < len(row) else ""
        out.append(rec)
    return out

@st.cache_data(show_spinner=False, ttl=300)
def load_roster(sheet_url_or_id: str, worksheet: str | None = None) -> List[Dict[str, Any]]:
    sheet_id = extract_sheet_id(sheet_url_or_id)
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1

    # First try: gspread records
    try:
        records = ws.get_all_records()
    except Exception:
        records = []

    out: List[Dict[str, Any]] = []
    if records:
        for r in records:
            nr = {normalize_key(str(k)): v for k, v in (r or {}).items()}
            out.append(nr)
        return out

    # Fallback: raw values with header detection
    values = ws.get_all_values()
    return _records_from_values(values)

def parse_dob(value) -> Any:
    """Parse DOB into a Python date if possible.

    Handles:
    - datetime/date objects
    - strings in common formats (YYYY-MM-DD, DD/MM/YYYY, etc.)
    - numeric Excel/Sheets serial dates (days since 1899-12-30)
    """
    if value is None or value == "":
        return None

    # Already a date/datetime
    try:
        import datetime as _dt
        if isinstance(value, _dt.datetime):
            return value.date()
        if isinstance(value, _dt.date):
            return value
    except Exception:
        pass

    # Numeric serial date (Sheets/Excel)
    try:
        if isinstance(value, (int, float)) and value > 0:
            import datetime as _dt
            origin = _dt.date(1899, 12, 30)
            if value < 60000:  # guard (~2064)
                return origin + _dt.timedelta(days=int(value))
    except Exception:
        pass

    # Try pandas parsing (dayfirst True then False)
    try:
        dt = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if not pd.isna(dt):
            return dt.date()
    except Exception:
        pass
    try:
        dt = pd.to_datetime(value, errors="coerce", dayfirst=False)
        if not pd.isna(dt):
            return dt.date()
    except Exception:
        pass

    return None

def last4_from_nric(nric: str) -> str:
    s = (nric or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    if len(s) >= 4:
        return s[-4:]
    return s
