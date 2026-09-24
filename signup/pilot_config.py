"""Google Sheets configuration repository for the SAA Streamlit pilot.

This module keeps the pilot's master/configuration data access outside the
Streamlit page so the storage layer can later be replaced by PostgreSQL/BigQuery
without rewriting the UI.

Expected worksheets in CONFIG_SHEET_URL:
    USERS, ORGANIZATIONS, COMPETITIONS, COMPETITION_FEES,
    COMPETITION_EVENTS, DIVISIONS

The reader deliberately tolerates the starter workbook layout where row 1 is a
sheet title and the real header row is lower down. It promotes the first row
containing the required columns to become the DataFrame header.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd
import streamlit as st

from google_sheets_reader import read_sheet_as_df


DEFAULT_TIMEZONE = "Asia/Singapore"


class PilotConfigError(RuntimeError):
    """Raised when pilot configuration is missing or internally inconsistent."""


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_header(value: Any) -> str:
    return _norm(value).upper().replace(" ", "_").replace("-", "_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = _norm(value).casefold()
    return s in {"true", "1", "yes", "y", "active"}


def _clean_id(value: Any) -> str:
    return _norm(value).upper()


def _promote_header_if_needed(
    df: pd.DataFrame,
    required_columns: set[str],
    worksheet: str,
) -> pd.DataFrame:
    """Return a DataFrame whose real worksheet header has been promoted.

    `read_sheet_as_df` normally expects the first worksheet row to be the
    header. The starter workbook intentionally contains a title row, so this
    function also scans the first few data rows for the real header.
    """
    if df is None:
        raise PilotConfigError(f"Worksheet {worksheet!r} returned no data.")

    out = df.copy()
    out.columns = [_norm_header(c) for c in out.columns]
    if required_columns.issubset(set(out.columns)):
        return out.dropna(how="all").reset_index(drop=True)

    scan_limit = min(len(out), 8)
    for i in range(scan_limit):
        row_values = [_norm_header(v) for v in out.iloc[i].tolist()]
        if required_columns.issubset(set(row_values)):
            new_columns = []
            seen: dict[str, int] = {}
            for j, raw in enumerate(row_values):
                base = raw or f"UNNAMED_{j + 1}"
                count = seen.get(base, 0)
                seen[base] = count + 1
                new_columns.append(base if count == 0 else f"{base}_{count + 1}")
            promoted = out.iloc[i + 1 :].copy()
            promoted.columns = new_columns
            return promoted.dropna(how="all").reset_index(drop=True)

    missing = ", ".join(sorted(required_columns - set(out.columns)))
    raise PilotConfigError(
        f"Worksheet {worksheet!r} does not expose the expected columns. "
        f"Missing: {missing}. Put the headers in row 1, or keep them within "
        "the first few rows so the pilot reader can detect them."
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_config_sheet(
    config_sheet_url: str,
    worksheet: str,
    required_columns: tuple[str, ...],
) -> pd.DataFrame:
    url = _norm(config_sheet_url)
    if not url:
        raise PilotConfigError("CONFIG_SHEET_URL is blank.")
    raw = read_sheet_as_df(url, worksheet=worksheet)
    return _promote_header_if_needed(raw, set(required_columns), worksheet)


def get_user_context(config_sheet_url: str, email: str) -> dict[str, Any] | None:
    """Return an active user joined to its active organisation."""
    users = load_config_sheet(
        config_sheet_url,
        "USERS",
        ("USER_ID", "EMAIL", "ORGANIZATION_ID", "ROLE", "ACTIVE"),
    ).copy()
    orgs = load_config_sheet(
        config_sheet_url,
        "ORGANIZATIONS",
        (
            "ORGANIZATION_ID",
            "ORGANIZATION_NAME",
            "TEAM_CODE",
            "ORGANIZATION_TYPE",
            "ACTIVE",
        ),
    ).copy()

    users["_EMAIL"] = users["EMAIL"].map(lambda x: _norm(x).casefold())
    users["_ACTIVE"] = users["ACTIVE"].map(_truthy)
    target = _norm(email).casefold()
    matches = users[(users["_EMAIL"] == target) & users["_ACTIVE"]]

    if matches.empty:
        return None
    if len(matches) > 1:
        raise PilotConfigError(
            f"USERS contains more than one active row for {email!r}. "
            "Each login email must map to exactly one active pilot user."
        )

    user = matches.iloc[0].to_dict()
    org_id = _clean_id(user.get("ORGANIZATION_ID"))

    orgs["_ORG_ID"] = orgs["ORGANIZATION_ID"].map(_clean_id)
    orgs["_ACTIVE"] = orgs["ACTIVE"].map(_truthy)
    org_matches = orgs[(orgs["_ORG_ID"] == org_id) & orgs["_ACTIVE"]]
    if org_matches.empty:
        raise PilotConfigError(
            f"User {email!r} references organisation {org_id!r}, but no active "
            "matching row exists in ORGANIZATIONS."
        )
    if len(org_matches) > 1:
        raise PilotConfigError(
            f"ORGANIZATIONS contains duplicate active rows for {org_id!r}."
        )

    org = org_matches.iloc[0].to_dict()
    return {
        "USER_ID": _norm(user.get("USER_ID")),
        "EMAIL": _norm(user.get("EMAIL")),
        "DISPLAY_NAME": _norm(user.get("DISPLAY_NAME")),
        "ROLE": _norm(user.get("ROLE")).upper(),
        "ORGANIZATION_ID": org_id,
        "ORGANIZATION_NAME": _norm(org.get("ORGANIZATION_NAME")),
        "TEAM_CODE": _norm(org.get("TEAM_CODE")),
        "ORGANIZATION_TYPE": _norm(org.get("ORGANIZATION_TYPE")).upper(),
    }


def _as_local_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    raw = _norm(value)
    if not raw:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize(tz)
        else:
            ts = ts.tz_convert(tz)
        return ts.to_pydatetime()
    return None


def determine_registration_period(
    competition_row: dict[str, Any],
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    """Return UPCOMING, NORMAL, LATE, PAUSED, or CLOSED."""
    tz = ZoneInfo(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)

    normal_open = _as_local_datetime(competition_row.get("NORMAL_OPEN_AT"), tz)
    normal_close = _as_local_datetime(competition_row.get("NORMAL_CLOSE_AT"), tz)
    late_open = _as_local_datetime(competition_row.get("LATE_OPEN_AT"), tz)
    late_close = _as_local_datetime(competition_row.get("LATE_CLOSE_AT"), tz)

    if normal_open and current < normal_open:
        return "UPCOMING"
    if normal_open and normal_close and normal_open <= current < normal_close:
        return "NORMAL"
    if late_open and late_close and late_open <= current < late_close:
        return "LATE"
    if normal_close and late_open and normal_close <= current < late_open:
        return "PAUSED"
    if late_close and current >= late_close:
        return "CLOSED"
    if normal_close and current >= normal_close and not late_open:
        return "CLOSED"
    return "CLOSED"


def get_competitions(
    config_sheet_url: str,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[dict[str, Any]]:
    df = load_config_sheet(
        config_sheet_url,
        "COMPETITIONS",
        (
            "COMPETITION_ID",
            "COMPETITION_NAME",
            "COMPETITION_TYPE",
            "NORMAL_OPEN_AT",
            "NORMAL_CLOSE_AT",
            "LATE_OPEN_AT",
            "LATE_CLOSE_AT",
            "ACTIVE",
        ),
    ).copy()
    df = df[df["ACTIVE"].map(_truthy)]
    rows: list[dict[str, Any]] = []
    for _, series in df.iterrows():
        row = series.to_dict()
        row["COMPETITION_ID"] = _clean_id(row.get("COMPETITION_ID"))
        row["COMPETITION_NAME"] = _norm(row.get("COMPETITION_NAME"))
        row["COMPETITION_TYPE"] = _norm(row.get("COMPETITION_TYPE")).upper()
        row["REGISTRATION_PERIOD"] = determine_registration_period(
            row, now=now, timezone_name=timezone_name
        )
        rows.append(row)
    return rows


def get_fee_per_entry(
    config_sheet_url: str,
    competition_id: str,
    organization_type: str,
    registration_period: str,
) -> float:
    period = _norm(registration_period).upper()
    if period not in {"NORMAL", "LATE"}:
        raise PilotConfigError(
            f"Cannot calculate an entry fee while registration period is {period!r}."
        )

    df = load_config_sheet(
        config_sheet_url,
        "COMPETITION_FEES",
        (
            "COMPETITION_ID",
            "ORGANIZATION_TYPE",
            "REGISTRATION_PERIOD",
            "FEE_PER_ENTRY_SGD",
            "ACTIVE",
        ),
    ).copy()
    mask = (
        df["COMPETITION_ID"].map(_clean_id).eq(_clean_id(competition_id))
        & df["ORGANIZATION_TYPE"].map(lambda x: _norm(x).upper()).eq(_norm(organization_type).upper())
        & df["REGISTRATION_PERIOD"].map(lambda x: _norm(x).upper()).eq(period)
        & df["ACTIVE"].map(_truthy)
    )
    matches = df[mask]
    if matches.empty:
        raise PilotConfigError(
            "No active COMPETITION_FEES row matches "
            f"competition={competition_id!r}, organisation_type={organization_type!r}, "
            f"period={period!r}."
        )
    if len(matches) > 1:
        raise PilotConfigError(
            "Duplicate active fee rows found for "
            f"competition={competition_id!r}, organisation_type={organization_type!r}, "
            f"period={period!r}."
        )
    value = pd.to_numeric(matches.iloc[0]["FEE_PER_ENTRY_SGD"], errors="coerce")
    if pd.isna(value):
        raise PilotConfigError("FEE_PER_ENTRY_SGD is not numeric for the selected fee row.")
    return float(value)


def get_competition_events(
    config_sheet_url: str,
    competition_id: str,
    gender: str,
    division_code: str,
) -> list[tuple[str, str]]:
    """Return [(event_name, event_code), ...] for one competition/gender/division."""
    df = load_config_sheet(
        config_sheet_url,
        "COMPETITION_EVENTS",
        (
            "COMPETITION_ID",
            "GENDER",
            "DIVISION_CODE",
            "EVENT_CODE",
            "EVENT_NAME",
            "ACTIVE",
        ),
    ).copy()
    mask = (
        df["COMPETITION_ID"].map(_clean_id).eq(_clean_id(competition_id))
        & df["GENDER"].map(lambda x: _norm(x).casefold()).eq(_norm(gender).casefold())
        & df["DIVISION_CODE"].map(_clean_id).eq(_clean_id(division_code))
        & df["ACTIVE"].map(_truthy)
    )
    subset = df[mask]
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, row in subset.iterrows():
        item = (_norm(row.get("EVENT_NAME")), _norm(row.get("EVENT_CODE")))
        if item[0] and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def get_divisions_for_competition(
    config_sheet_url: str,
    competition_id: str,
    gender: str | None = None,
) -> list[dict[str, Any]]:
    divisions = load_config_sheet(
        config_sheet_url,
        "DIVISIONS",
        (
            "DIVISION_CODE",
            "DIVISION_NAME",
            "MIN_AGE",
            "MAX_AGE",
            "DISPLAY_ORDER",
            "ACTIVE",
        ),
    ).copy()
    events = load_config_sheet(
        config_sheet_url,
        "COMPETITION_EVENTS",
        ("COMPETITION_ID", "GENDER", "DIVISION_CODE", "ACTIVE"),
    ).copy()

    event_mask = (
        events["COMPETITION_ID"].map(_clean_id).eq(_clean_id(competition_id))
        & events["ACTIVE"].map(_truthy)
    )
    if _norm(gender):
        event_mask &= events["GENDER"].map(lambda x: _norm(x).casefold()).eq(_norm(gender).casefold())
    allowed_codes = set(events.loc[event_mask, "DIVISION_CODE"].map(_clean_id))

    divisions = divisions[
        divisions["ACTIVE"].map(_truthy)
        & divisions["DIVISION_CODE"].map(_clean_id).isin(allowed_codes)
    ].copy()
    divisions["_ORDER"] = pd.to_numeric(divisions["DISPLAY_ORDER"], errors="coerce").fillna(9999)
    divisions = divisions.sort_values(["_ORDER", "DIVISION_CODE"], kind="stable")

    out: list[dict[str, Any]] = []
    for _, row in divisions.iterrows():
        min_age = pd.to_numeric(row.get("MIN_AGE"), errors="coerce")
        max_age = pd.to_numeric(row.get("MAX_AGE"), errors="coerce")
        out.append(
            {
                "DIVISION_CODE": _clean_id(row.get("DIVISION_CODE")),
                "DIVISION_NAME": _norm(row.get("DIVISION_NAME")),
                "MIN_AGE": None if pd.isna(min_age) else int(min_age),
                "MAX_AGE": None if pd.isna(max_age) else int(max_age),
                "DISPLAY_ORDER": int(row.get("_ORDER", 9999)),
            }
        )
    return out


def eligible_divisions_for_birth_year(
    divisions: list[dict[str, Any]],
    birth_date: date | None,
    competition_start_at: Any,
) -> list[dict[str, Any]]:
    """Filter divisions using the agreed year-of-birth age rule.

    Age is calculated as competition year - birth year, not by birthday reached.
    OPEN can therefore overlap U18/U20 when its age band also matches.
    """
    if not birth_date:
        return divisions

    comp_ts = pd.to_datetime(competition_start_at, errors="coerce")
    if pd.isna(comp_ts):
        raise PilotConfigError("COMPETITION_START_AT is missing or invalid for the selected competition.")
    age = int(comp_ts.year) - int(birth_date.year)

    eligible: list[dict[str, Any]] = []
    for row in divisions:
        lo = row.get("MIN_AGE")
        hi = row.get("MAX_AGE")
        if lo is not None and age < int(lo):
            continue
        if hi is not None and age > int(hi):
            continue
        eligible.append(row)
    return eligible
