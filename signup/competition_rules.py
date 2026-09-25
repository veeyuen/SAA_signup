"""Pure competition/division/event eligibility rules for registration.

Phase 5C keeps rule evaluation outside the Streamlit UI so the same checks can
run when an athlete is added to the cart and again immediately before an order
or payment is created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class CompetitionRuleIssue:
    code: str
    message: str
    athlete_name: str = ""
    competition_id: str = ""
    division_code: str = ""
    event_name: str = ""
    event_code: str = ""
    athlete_age: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "CODE": self.code,
            "MESSAGE": self.message,
            "ATHLETE_NAME": self.athlete_name,
            "COMPETITION_ID": self.competition_id,
            "DIVISION_CODE": self.division_code,
            "EVENT_NAME": self.event_name,
            "EVENT_CODE": self.event_code,
            "ATHLETE_AGE": self.athlete_age,
        }


@dataclass
class CompetitionRuleResult:
    issues: list[CompetitionRuleIssue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.issues)

    @property
    def codes(self) -> set[str]:
        return {issue.code for issue in self.issues}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _key(value: Any) -> str:
    return _clean(value).casefold()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = _key(value)
    if raw in {"true", "1", "yes", "y", "active"}:
        return True
    if raw in {"false", "0", "no", "n", "inactive"}:
        return False
    return default


def _gender_key(value: Any) -> str:
    raw = _key(value)
    if raw in {"m", "male", "men", "boy", "boys"}:
        return "M"
    if raw in {"f", "female", "women", "woman", "girl", "girls"}:
        return "F"
    return _clean(value).upper()


def _normalise_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key).strip().upper(): value for key, value in dict(row).items()}


def _records(data: Any) -> tuple[list[dict[str, Any]], set[str]]:
    """Return normalized records + known columns from DataFrame/list-like input."""
    if data is None:
        return [], set()

    columns: set[str] = set()
    if hasattr(data, "columns") and hasattr(data, "to_dict"):
        columns = {str(c).strip().upper() for c in data.columns}
        raw_records = data.to_dict("records")
    else:
        raw_records = list(data)
        for row in raw_records:
            if isinstance(row, Mapping):
                columns.update(str(k).strip().upper() for k in row.keys())

    records = [
        _normalise_record(row)
        for row in raw_records
        if isinstance(row, Mapping)
    ]
    return records, columns


def _as_date(value: Any) -> dt.date | None:
    if value is None or _clean(value) == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    # pandas Timestamp and similar datetime objects expose date().
    date_method = getattr(value, "date", None)
    if callable(date_method):
        try:
            parsed = date_method()
            if isinstance(parsed, dt.date):
                return parsed
        except Exception:
            pass

    text = _clean(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        pass

    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def age_on_date(birth_date: Any, as_of: Any) -> int | None:
    """Return completed age on ``as_of`` or None if either date is invalid."""
    birth = _as_date(birth_date)
    check_date = _as_date(as_of)
    if birth is None or check_date is None or birth > check_date:
        return None
    return (
        check_date.year
        - birth.year
        - ((check_date.month, check_date.day) < (birth.month, birth.day))
    )


def _strict_int(value: Any, *, blank_allowed: bool) -> tuple[int | None, bool]:
    raw = _clean(value)
    if raw == "":
        return (None, blank_allowed)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None, False
    if not number.is_integer():
        return None, False
    return int(number), True


def _missing_columns(columns: set[str], required: Iterable[str]) -> list[str]:
    return [column for column in required if column not in columns]


def _config_issue(
    message: str,
    *,
    competition_id: str = "",
    athlete_name: str = "",
    division_code: str = "",
    event_name: str = "",
    event_code: str = "",
) -> CompetitionRuleIssue:
    return CompetitionRuleIssue(
        code="CONFIG_ERROR",
        message=message,
        athlete_name=athlete_name,
        competition_id=competition_id,
        division_code=division_code,
        event_name=event_name,
        event_code=event_code,
    )


def _competition_start_date(
    competition_id: str,
    competition_rows: Any,
) -> tuple[dt.date | None, list[CompetitionRuleIssue]]:
    records, columns = _records(competition_rows)
    required = {
        "COMPETITION_ID",
        "COMPETITION_START_AT",
        "STATUS",
        "ACTIVE",
    }
    missing = _missing_columns(columns, required)
    if missing:
        return None, [
            _config_issue(
                "COMPETITIONS is missing required column(s): " + ", ".join(missing),
                competition_id=competition_id,
            )
        ]

    matches = [
        row
        for row in records
        if _key(row.get("COMPETITION_ID")) == _key(competition_id)
    ]
    if len(matches) != 1:
        return None, [
            _config_issue(
                "Expected exactly one COMPETITIONS row for "
                f"{competition_id!r}; found {len(matches)}.",
                competition_id=competition_id,
            )
        ]

    row = matches[0]
    status = _clean(row.get("STATUS")).upper()
    if not _as_bool(row.get("ACTIVE"), False) or status in {"CANCELLED", "INACTIVE"}:
        return None, [
            CompetitionRuleIssue(
                code="COMPETITION_UNAVAILABLE",
                message="This competition is no longer active for registration.",
                competition_id=competition_id,
            )
        ]

    start_date = _as_date(row.get("COMPETITION_START_AT"))
    if start_date is None:
        return None, [
            _config_issue(
                "COMPETITION_START_AT is missing or invalid; age eligibility cannot be established.",
                competition_id=competition_id,
            )
        ]
    return start_date, []


def validate_athlete_selection(
    *,
    competition_id: str,
    competition_start_at: Any,
    athlete_name: str,
    birth_date: Any,
    gender: str,
    division_code: str,
    events: Iterable[Mapping[str, Any]],
    division_rows: Any,
    competition_event_rows: Any,
) -> CompetitionRuleResult:
    """Validate one athlete's division + events against configured master data."""
    result = CompetitionRuleResult()
    competition_id = _clean(competition_id)
    athlete_name = _clean(athlete_name)
    division_code = _clean(division_code)

    athlete_age = age_on_date(birth_date, competition_start_at)
    if athlete_age is None:
        result.issues.append(
            CompetitionRuleIssue(
                code="DIVISION_INELIGIBLE",
                message=(
                    "Division eligibility cannot be established because the athlete's "
                    "date of birth is missing, invalid, or after the competition date."
                ),
                athlete_name=athlete_name,
                competition_id=competition_id,
                division_code=division_code,
            )
        )
        return result

    div_records, div_columns = _records(division_rows)
    required_div = {"DIVISION_CODE", "MIN_AGE", "MAX_AGE", "ACTIVE"}
    missing_div = _missing_columns(div_columns, required_div)
    if missing_div:
        result.issues.append(
            _config_issue(
                "DIVISIONS is missing required column(s): " + ", ".join(missing_div),
                competition_id=competition_id,
                athlete_name=athlete_name,
                division_code=division_code,
            )
        )
        return result

    matching_divisions = [
        row
        for row in div_records
        if _key(row.get("DIVISION_CODE")) == _key(division_code)
        and _as_bool(row.get("ACTIVE"), False)
    ]
    if len(matching_divisions) != 1:
        if len(matching_divisions) == 0:
            result.issues.append(
                CompetitionRuleIssue(
                    code="DIVISION_INELIGIBLE",
                    message=f"Division {division_code or '(blank)'} is not active.",
                    athlete_name=athlete_name,
                    competition_id=competition_id,
                    division_code=division_code,
                    athlete_age=athlete_age,
                )
            )
        else:
            result.issues.append(
                _config_issue(
                    f"DIVISIONS contains {len(matching_divisions)} active rows for {division_code!r}.",
                    competition_id=competition_id,
                    athlete_name=athlete_name,
                    division_code=division_code,
                )
            )
        return result

    division = matching_divisions[0]
    min_age, min_valid = _strict_int(division.get("MIN_AGE"), blank_allowed=False)
    max_age, max_valid = _strict_int(division.get("MAX_AGE"), blank_allowed=True)
    if not min_valid or min_age is None or not max_valid or (
        max_age is not None and max_age < min_age
    ):
        result.issues.append(
            _config_issue(
                f"DIVISIONS has invalid MIN_AGE/MAX_AGE for {division_code!r}.",
                competition_id=competition_id,
                athlete_name=athlete_name,
                division_code=division_code,
            )
        )
        return result

    if athlete_age < min_age or (max_age is not None and athlete_age > max_age):
        age_range = f"{min_age}+" if max_age is None else f"{min_age}-{max_age}"
        result.issues.append(
            CompetitionRuleIssue(
                code="DIVISION_INELIGIBLE",
                message=(
                    f"{athlete_name or 'This athlete'} is age {athlete_age} on the "
                    f"competition start date and is not eligible for {division_code} "
                    f"(configured age range {age_range})."
                ),
                athlete_name=athlete_name,
                competition_id=competition_id,
                division_code=division_code,
                athlete_age=athlete_age,
            )
        )
        return result

    event_records, event_columns = _records(competition_event_rows)
    required_events = {
        "COMPETITION_ID",
        "GENDER",
        "DIVISION_CODE",
        "EVENT_CODE",
        "EVENT_NAME",
        "ACTIVE",
    }
    missing_events = _missing_columns(event_columns, required_events)
    if missing_events:
        result.issues.append(
            _config_issue(
                "COMPETITION_EVENTS is missing required column(s): "
                + ", ".join(missing_events),
                competition_id=competition_id,
                athlete_name=athlete_name,
                division_code=division_code,
            )
        )
        return result

    requested_gender = _gender_key(gender)
    if requested_gender not in {"M", "F"}:
        result.issues.append(
            CompetitionRuleIssue(
                code="EVENT_INELIGIBLE",
                message="A valid athlete gender is required to determine event eligibility.",
                athlete_name=athlete_name,
                competition_id=competition_id,
                division_code=division_code,
                athlete_age=athlete_age,
            )
        )
        return result

    scoped_events = [
        row
        for row in event_records
        if _as_bool(row.get("ACTIVE"), False)
        and _key(row.get("COMPETITION_ID")) == _key(competition_id)
        and _gender_key(row.get("GENDER")) == requested_gender
        and _key(row.get("DIVISION_CODE")) == _key(division_code)
    ]

    requested_events = [dict(event) for event in (events or [])]
    if not requested_events:
        result.issues.append(
            CompetitionRuleIssue(
                code="EVENT_INELIGIBLE",
                message="At least one configured event is required.",
                athlete_name=athlete_name,
                competition_id=competition_id,
                division_code=division_code,
                athlete_age=athlete_age,
            )
        )
        return result

    for event in requested_events:
        event_name = _clean(event.get("event_name") or event.get("EVENT_NAME"))
        event_code = _clean(event.get("event_code") or event.get("EVENT_CODE"))

        if event_code:
            matches = [
                row
                for row in scoped_events
                if _key(row.get("EVENT_CODE")) == _key(event_code)
            ]
            # Event code is the stable key. A label/name change alone does not
            # invalidate a cart item if the same code is still configured.
            distinct_names = {
                _key(row.get("EVENT_NAME")) for row in matches if _clean(row.get("EVENT_NAME"))
            }
            if len(distinct_names) > 1:
                result.issues.append(
                    _config_issue(
                        f"COMPETITION_EVENTS maps event code {event_code!r} to multiple active names.",
                        competition_id=competition_id,
                        athlete_name=athlete_name,
                        division_code=division_code,
                        event_name=event_name,
                        event_code=event_code,
                    )
                )
                continue
        else:
            matches = [
                row
                for row in scoped_events
                if _key(row.get("EVENT_NAME")) == _key(event_name)
            ]
            distinct_codes = {
                _key(row.get("EVENT_CODE")) for row in matches if _clean(row.get("EVENT_CODE"))
            }
            if len(distinct_codes) > 1:
                result.issues.append(
                    _config_issue(
                        f"COMPETITION_EVENTS maps event name {event_name!r} to multiple active codes.",
                        competition_id=competition_id,
                        athlete_name=athlete_name,
                        division_code=division_code,
                        event_name=event_name,
                    )
                )
                continue

        if not matches:
            result.issues.append(
                CompetitionRuleIssue(
                    code="EVENT_INELIGIBLE",
                    message=(
                        f"{event_name or event_code or 'The selected event'} is not an active "
                        f"event for {gender} / {division_code} in this competition."
                    ),
                    athlete_name=athlete_name,
                    competition_id=competition_id,
                    division_code=division_code,
                    event_name=event_name,
                    event_code=event_code,
                    athlete_age=athlete_age,
                )
            )

    return result


def validate_cart_competition_rules(
    cart_items: Iterable[Mapping[str, Any]],
    *,
    competition_id: str,
    competition_rows: Any,
    division_rows: Any,
    competition_event_rows: Any,
) -> CompetitionRuleResult:
    """Validate every cart item against one current competition configuration."""
    competition_id = _clean(competition_id)
    result = CompetitionRuleResult()

    start_date, competition_issues = _competition_start_date(
        competition_id, competition_rows
    )
    result.issues.extend(competition_issues)
    if start_date is None:
        return result

    for item in cart_items or []:
        item = dict(item)
        athlete_name = _clean(item.get("athlete_name"))
        item_division = _clean(item.get("division"))
        rows = [dict(row) for row in (item.get("entry_rows") or [])]
        if not rows:
            result.issues.append(
                _config_issue(
                    "A cart athlete contains no event-entry rows.",
                    competition_id=competition_id,
                    athlete_name=athlete_name,
                    division_code=item_division,
                )
            )
            continue

        first = rows[0]
        row_competition_ids = {
            _key(row.get("competition_id")) for row in rows
        }
        if row_competition_ids != {_key(competition_id)}:
            result.issues.append(
                _config_issue(
                    "Cart entry competition IDs do not match the selected competition.",
                    competition_id=competition_id,
                    athlete_name=athlete_name,
                    division_code=item_division,
                )
            )
            continue

        row_divisions = {_key(row.get("event_division")) for row in rows}
        if len(row_divisions) != 1 or _key(item_division) not in row_divisions:
            result.issues.append(
                _config_issue(
                    "Cart entry division values are inconsistent for this athlete.",
                    competition_id=competition_id,
                    athlete_name=athlete_name,
                    division_code=item_division,
                )
            )
            continue

        row_dobs = {_clean(row.get("birth_date")) for row in rows}
        row_genders = {_gender_key(row.get("gender", "")) for row in rows}
        if len(row_dobs) != 1 or len(row_genders) != 1:
            result.issues.append(
                _config_issue(
                    "Cart entry DOB/gender values are inconsistent for this athlete.",
                    competition_id=competition_id,
                    athlete_name=athlete_name,
                    division_code=item_division,
                )
            )
            continue

        athlete_result = validate_athlete_selection(
            competition_id=competition_id,
            competition_start_at=start_date,
            athlete_name=athlete_name,
            birth_date=first.get("birth_date"),
            gender=first.get("gender", ""),
            division_code=item_division,
            events=[
                {
                    "event_name": row.get("event", ""),
                    "event_code": row.get("event_code", ""),
                }
                for row in rows
            ],
            division_rows=division_rows,
            competition_event_rows=competition_event_rows,
        )
        result.issues.extend(athlete_result.issues)

    return result
