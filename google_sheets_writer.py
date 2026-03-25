"""
Google Sheets writer for syncing "Current entries" from Streamlit.

Uses Streamlit secrets:
- st.secrets["gcp_service_account"] : Google service account JSON dict

Requires:
- gspread
- google-auth
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
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


@st.cache_resource(show_spinner=False)
def _get_gspread_client_write():
    if gspread is None or service_account is None:
        raise RuntimeError("Missing dependencies: install 'gspread' and 'google-auth'.")

    if not (hasattr(st, "secrets") and "gcp_service_account" in st.secrets and st.secrets["gcp_service_account"]):
        raise RuntimeError("Missing st.secrets['gcp_service_account'] for Google Sheets access.")

    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def _to_serializable(v: Any) -> Any:
    if v is None:
        return ""
    # date / datetime -> ISO string
    try:
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return v.date().isoformat()
        if isinstance(v, _dt.date):
            return v.isoformat()
    except Exception:
        pass
    # pandas Timestamp -> date
    try:
        if hasattr(v, "to_pydatetime"):
            return v.to_pydatetime().date().isoformat()
    except Exception:
        pass
    return str(v)


def sync_entries_to_sheet(
    entries: List[Dict[str, Any]],
    *,
    sheet_url_or_id: str,
    worksheet: Optional[str] = None,
    column_order: Optional[List[str]] = None,
) -> None:
    """Overwrite the target worksheet with the current entries table."""
    sheet_id = extract_sheet_id(sheet_url_or_id)
    gc = _get_gspread_client_write()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1

    df = pd.DataFrame(entries or [])
    if df.empty:
        # Keep headers if provided, else just clear
        ws.clear()
        return

    if column_order:
        # Ensure all cols exist
        for c in column_order:
            if c not in df.columns:
                df[c] = ""
        # Put preferred cols first
        cols = [c for c in column_order if c in df.columns] + [c for c in df.columns if c not in column_order]
        df = df[cols]

    # Convert values to strings/serializable
    values = [list(df.columns)]
    for _, row in df.iterrows():
        values.append([_to_serializable(row.get(c)) for c in df.columns])

    ws.clear()
    # gspread API signature varies by version; try both.
    try:
        ws.update("A1", values)
    except TypeError:
        ws.update(values, "A1")
