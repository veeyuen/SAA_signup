"""Google Sheets-backed master/configuration repository for the SAA signup pilot.

This module intentionally handles only relatively small configuration tables:
USERS, ORGANIZATIONS, COMPETITIONS, COMPETITION_FEES, DIVISIONS and
COMPETITION_EVENTS. Transactional registration/payment storage remains separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from google_sheets_reader import read_sheet_as_df


SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


class PilotConfigError(RuntimeError):
    """Raised when required pilot configuration is missing or inconsistent."""


def _clean(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _upper(value) -> str:
    return _clean(value).upper()


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = _clean(value).casefold()
    if raw in {"true", "1", "yes", "y", "active"}:
        return True
    if raw in {"false", "0", "no", "n", "inactive"}:
        return False
    return default


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().upper() for c in out.columns]
    return out


def _require_columns(df: pd.DataFrame, sheet: str, columns: list[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise PilotConfigError(
            f"{sheet} is missing required column(s): {', '.join(missing)}"
        )


def _sg_timestamp(value):
    """Parse a Sheet timestamp and interpret timezone-naive values as Singapore local."""
    if value is None or _clean(value) == "":
        return None

    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None

    if isinstance(ts, pd.DatetimeIndex):
        ts = ts[0]

    if getattr(ts, "tzinfo", None) is None:
        return ts.tz_localize(SINGAPORE_TZ)
    return ts.tz_convert(SINGAPORE_TZ)


@st.cache_data(ttl=120, show_spinner=False)
def _read_config_sheet(sheet_url: str, worksheet: str) -> pd.DataFrame:
    df = read_sheet_as_df(sheet_url, worksheet=worksheet)
    if df is None:
        return pd.DataFrame()
    return _normalise_columns(pd.DataFrame(df))


@dataclass(frozen=True)
class ConfiguredUser:
    user_id: str
    email: str
    display_name: str
    organization_id: str
    role: str


@dataclass(frozen=True)
class Organization:
    organization_id: str
    organization_name: str
    team_code: str
    organization_type: str


@dataclass(frozen=True)
class Competition:
    competition_id: str
    competition_name: str
    competition_type: str
    competition_start_at: object
    normal_open_at: object
    normal_close_at: object
    late_open_at: object
    late_close_at: object
    status: str


class PilotConfigRepository:
    def __init__(self, sheet_url: str):
        self.sheet_url = _clean(sheet_url)
        if not self.sheet_url:
            raise PilotConfigError(
                "CONFIG_SHEET_URL is missing from Streamlit secrets."
            )

    def table(self, worksheet: str) -> pd.DataFrame:
        try:
            return _read_config_sheet(self.sheet_url, worksheet)
        except Exception as exc:
            raise PilotConfigError(
                f"Could not read configuration worksheet '{worksheet}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def get_user(self, email: str) -> ConfiguredUser | None:
        df = self.table("USERS")
        _require_columns(
            df,
            "USERS",
            ["USER_ID", "EMAIL", "ORGANIZATION_ID", "ROLE", "ACTIVE"],
        )

        email_cf = _clean(email).casefold()
        matches = df[
            df["EMAIL"].map(lambda x: _clean(x).casefold() == email_cf)
            & df["ACTIVE"].map(lambda x: _as_bool(x, False))
        ]

        if len(matches) == 0:
            return None
        if len(matches) > 1:
            raise PilotConfigError(
                f"USERS contains more than one active row for {email}."
            )

        row = matches.iloc[0]
        return ConfiguredUser(
            user_id=_clean(row["USER_ID"]),
            email=_clean(row["EMAIL"]).lower(),
            display_name=_clean(row.get("DISPLAY_NAME", "")),
            organization_id=_clean(row["ORGANIZATION_ID"]),
            role=_upper(row["ROLE"]),
        )

    def get_organization(self, organization_id: str) -> Organization:
        df = self.table("ORGANIZATIONS")
        _require_columns(
            df,
            "ORGANIZATIONS",
            [
                "ORGANIZATION_ID",
                "ORGANIZATION_NAME",
                "TEAM_CODE",
                "ORGANIZATION_TYPE",
                "ACTIVE",
            ],
        )

        matches = df[
            df["ORGANIZATION_ID"].map(_clean).eq(_clean(organization_id))
            & df["ACTIVE"].map(lambda x: _as_bool(x, False))
        ]
        if len(matches) != 1:
            raise PilotConfigError(
                "Expected exactly one active ORGANIZATIONS row for "
                f"ORGANIZATION_ID={organization_id!r}; found {len(matches)}."
            )

        row = matches.iloc[0]
        return Organization(
            organization_id=_clean(row["ORGANIZATION_ID"]),
            organization_name=_clean(row["ORGANIZATION_NAME"]),
            team_code=_clean(row["TEAM_CODE"]),
            organization_type=_upper(row["ORGANIZATION_TYPE"]),
        )

    @staticmethod
    def registration_period(competition: Competition, now=None) -> str:
        now = now or pd.Timestamp.now(tz=SINGAPORE_TZ)

        no = competition.normal_open_at
        nc = competition.normal_close_at
        lo = competition.late_open_at
        lc = competition.late_close_at

        if no is not None and nc is not None and no <= now < nc:
            return "NORMAL"
        if lo is not None and lc is not None and lo <= now < lc:
            return "LATE"
        if no is not None and now < no:
            return "UPCOMING"
        return "CLOSED"

    def competitions(self, include_closed: bool = False) -> list[Competition]:
        df = self.table("COMPETITIONS")
        _require_columns(
            df,
            "COMPETITIONS",
            [
                "COMPETITION_ID",
                "COMPETITION_NAME",
                "COMPETITION_TYPE",
                "COMPETITION_START_AT",
                "NORMAL_OPEN_AT",
                "NORMAL_CLOSE_AT",
                "LATE_OPEN_AT",
                "LATE_CLOSE_AT",
                "STATUS",
                "ACTIVE",
            ],
        )

        out: list[Competition] = []
        for _, row in df.iterrows():
            if not _as_bool(row.get("ACTIVE"), False):
                continue

            status = _upper(row.get("STATUS"))
            if status in {"CANCELLED", "INACTIVE"}:
                continue

            comp = Competition(
                competition_id=_clean(row["COMPETITION_ID"]),
                competition_name=_clean(row["COMPETITION_NAME"]),
                competition_type=_upper(row["COMPETITION_TYPE"]),
                competition_start_at=_sg_timestamp(row["COMPETITION_START_AT"]),
                normal_open_at=_sg_timestamp(row["NORMAL_OPEN_AT"]),
                normal_close_at=_sg_timestamp(row["NORMAL_CLOSE_AT"]),
                late_open_at=_sg_timestamp(row["LATE_OPEN_AT"]),
                late_close_at=_sg_timestamp(row["LATE_CLOSE_AT"]),
                status=status,
            )

            period = self.registration_period(comp)
            if include_closed or period in {"NORMAL", "LATE"}:
                out.append(comp)

        out.sort(
            key=lambda c: (
                c.competition_start_at
                if c.competition_start_at is not None
                else pd.Timestamp.max.tz_localize(SINGAPORE_TZ)
            )
        )
        return out

    def fee_for(
        self,
        competition_id: str,
        organization_type: str,
        registration_period: str,
    ) -> Decimal:
        df = self.table("COMPETITION_FEES")
        _require_columns(
            df,
            "COMPETITION_FEES",
            [
                "COMPETITION_ID",
                "ORGANIZATION_TYPE",
                "REGISTRATION_PERIOD",
                "FEE_PER_ENTRY_SGD",
                "ACTIVE",
            ],
        )

        matches = df[
            df["COMPETITION_ID"].map(_clean).eq(_clean(competition_id))
            & df["ORGANIZATION_TYPE"].map(_upper).eq(_upper(organization_type))
            & df["REGISTRATION_PERIOD"].map(_upper).eq(_upper(registration_period))
            & df["ACTIVE"].map(lambda x: _as_bool(x, False))
        ]

        if len(matches) != 1:
            raise PilotConfigError(
                "Expected exactly one active COMPETITION_FEES row for "
                f"{competition_id} / {organization_type} / {registration_period}; "
                f"found {len(matches)}."
            )

        raw = _clean(matches.iloc[0]["FEE_PER_ENTRY_SGD"]).replace("$", "").replace(",", "")
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise PilotConfigError(
                f"Invalid FEE_PER_ENTRY_SGD value {raw!r} for {competition_id}."
            ) from exc

    def division_rows(self, competition_id: str, gender: str = "") -> list[dict]:
        events = self.table("COMPETITION_EVENTS")
        _require_columns(
            events,
            "COMPETITION_EVENTS",
            [
                "COMPETITION_ID",
                "GENDER",
                "DIVISION_CODE",
                "EVENT_CODE",
                "EVENT_NAME",
                "ACTIVE",
            ],
        )

        mask = (
            events["COMPETITION_ID"].map(_clean).eq(_clean(competition_id))
            & events["ACTIVE"].map(lambda x: _as_bool(x, False))
        )
        if _clean(gender):
            mask &= events["GENDER"].map(lambda x: _clean(x).casefold()).eq(
                _clean(gender).casefold()
            )

        codes = []
        seen = set()
        for value in events.loc[mask, "DIVISION_CODE"]:
            code = _clean(value)
            if code and code not in seen:
                codes.append(code)
                seen.add(code)

        labels = {}
        order = {}
        try:
            divs = self.table("DIVISIONS")
            _require_columns(
                divs,
                "DIVISIONS",
                ["DIVISION_CODE", "DIVISION_NAME", "ACTIVE"],
            )
            for _, row in divs.iterrows():
                if not _as_bool(row.get("ACTIVE"), False):
                    continue
                code = _clean(row.get("DIVISION_CODE"))
                labels[code] = _clean(row.get("DIVISION_NAME")) or code
                try:
                    order[code] = int(float(_clean(row.get("DISPLAY_ORDER")) or 9999))
                except ValueError:
                    order[code] = 9999
        except PilotConfigError:
            raise

        codes.sort(key=lambda code: (order.get(code, 9999), code))
        return [
            {
                "code": code,
                "label": labels.get(code, code),
            }
            for code in codes
        ]

    def event_options(
        self,
        competition_id: str,
        gender: str,
        division_code: str,
    ) -> list[tuple[str, str]]:
        if not _clean(gender) or not _clean(division_code):
            return []

        df = self.table("COMPETITION_EVENTS")
        _require_columns(
            df,
            "COMPETITION_EVENTS",
            [
                "COMPETITION_ID",
                "GENDER",
                "DIVISION_CODE",
                "EVENT_CODE",
                "EVENT_NAME",
                "ACTIVE",
            ],
        )

        matches = df[
            df["COMPETITION_ID"].map(_clean).eq(_clean(competition_id))
            & df["GENDER"].map(lambda x: _clean(x).casefold()).eq(
                _clean(gender).casefold()
            )
            & df["DIVISION_CODE"].map(_clean).eq(_clean(division_code))
            & df["ACTIVE"].map(lambda x: _as_bool(x, False))
        ]

        out = []
        seen = set()
        for _, row in matches.iterrows():
            name = _clean(row["EVENT_NAME"])
            code = _clean(row["EVENT_CODE"])
            key = (name, code)
            if name and key not in seen:
                out.append((name, code))
                seen.add(key)
        return out


def require_configured_user(
    repository: PilotConfigRepository,
    app_title: str,
    provider: str = "auth0",
) -> tuple[str, ConfiguredUser, Organization]:
    """Authenticate with Streamlit OIDC, then authorise using USERS/ORGANIZATIONS."""
    if not getattr(st, "user", None) or not st.user.is_logged_in:
        st.title(app_title)
        st.info("Please log in with email to continue.")
        st.button("Log in with email", on_click=lambda: st.login(provider))
        st.stop()

    user_email = _clean(getattr(st.user, "email", "")).lower()
    if not user_email:
        st.error(
            "Login succeeded but no email address was returned. "
            "Please contact the administrator."
        )
        st.button("Log out", on_click=st.logout)
        st.stop()

    try:
        user = repository.get_user(user_email)
        if user is None:
            st.error(
                "You are logged in, but your email is not registered in the "
                f"USERS worksheet. Access denied for {user_email}."
            )
            st.button("Log out", on_click=st.logout)
            st.stop()

        if user.role not in {"ENTRY_ADMIN", "SAA_ADMIN"}:
            st.error(
                f"Your configured role ({user.role or 'blank'}) does not permit entry access."
            )
            st.button("Log out", on_click=st.logout)
            st.stop()

        organization = repository.get_organization(user.organization_id)
    except PilotConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()

    with st.sidebar:
        st.caption(f"Logged in as: {user_email}")
        st.caption(
            f"Organisation: {organization.organization_name} "
            f"({organization.organization_type})"
        )
        st.button("Log out", on_click=st.logout)

    return user_email, user, organization
