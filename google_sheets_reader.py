"""Google Sheets reader for exporting semicolon-delimited files."""

from __future__ import annotations
from typing import Optional
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
    return m.group(1) if m else s

@st.cache_resource(show_spinner=False)
def get_gspread_client_read():
    if gspread is None or service_account is None:
        raise RuntimeError("Missing dependencies: install 'gspread' and 'google-auth'.")
    if not (hasattr(st, "secrets") and "gcp_service_account" in st.secrets and st.secrets["gcp_service_account"]):
        raise RuntimeError("Missing st.secrets['gcp_service_account'] for Google Sheets access.")
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)

@st.cache_data(show_spinner=False, ttl=30)
def read_sheet_as_df(sheet_url_or_id: str, worksheet: Optional[str] = None) -> pd.DataFrame:
    sheet_id = extract_sheet_id(sheet_url_or_id)
    gc = get_gspread_client_read()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    header = values[0]
    rows = values[1:]
    if not any((h or "").strip() for h in header):
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=header)
