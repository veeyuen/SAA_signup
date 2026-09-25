"""Competition-wide athlete registration integrity checks for the SAA pilot.

The Google Sheets pilot cannot enforce relational uniqueness at database level, so
this module provides application-level checks immediately before a cart item is
added and again immediately before order submission.

Rules implemented for one competition:
- Same athlete + same event => block duplicate entry.
- Same athlete + different team/organisation => block team conflict.
- Same athlete + same team + different event => allow.
- Same normalised name + DOB but two different non-empty athlete IDs => block as
  an identity collision for SA Events review; never silently merge the records.
- Withdrawn/deleted/cancelled entries do not block a new registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Iterable, Mapping


_TRUE_VALUES = {"1", "true", "yes", "y"}
_INACTIVE_ENTRY_STATUSES = {
    "WITHDRAWN",
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "VOID",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _clean(value).casefold()


def normalise_person_name(value: Any) -> str:
    """Return a conservative exact-comparison name key.

    Punctuation and repeated whitespace are normalised, but token order is not
    changed. This intentionally avoids fuzzy matching during registration.
    """
    text = unicodedata.normalize("NFKD", _clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() else " " for ch in text.upper())
    return " ".join(text.split())


def _normalise_dob(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    # Transaction rows use ISO dates. Keep only the date component when a
    # timestamp happens to be supplied by an older record.
    return text[:10]


def _normalise_event(row: Mapping[str, Any]) -> str:
    # Event codes in legacy athletics lists are not guaranteed globally unique,
    # so the canonical event name is the safer primary key. Fall back to code
    # only for historical rows where the name is missing.
    name = _norm(row.get("EVENT_NAME") or row.get("event_name") or row.get("event"))
    if name:
        return f"name:{name}"
    code = _norm(row.get("EVENT_CODE") or row.get("event_code"))
    return f"code:{code}" if code else ""


def _normalise_team(row: Mapping[str, Any]) -> str:
    team_code = _norm(row.get("TEAM_CODE") or row.get("team_code"))
    if team_code:
        return f"team:{team_code}"
    org = _norm(row.get("ORGANIZATION_ID") or row.get("organization_id"))
    return f"org:{org}" if org else ""


def _is_deleted(row: Mapping[str, Any]) -> bool:
    return _norm(row.get("IS_DELETED") or row.get("is_deleted")) in _TRUE_VALUES


def is_active_existing_entry(row: Mapping[str, Any]) -> bool:
    if _is_deleted(row):
        return False
    status = _clean(row.get("STATUS") or row.get("status")).upper()
    return status not in _INACTIVE_ENTRY_STATUSES


@dataclass(frozen=True)
class IntegrityConflict:
    code: str
    message: str
    existing_entry_id: str = ""
    existing_order_id: str = ""
    existing_registration_id: str = ""
    existing_organization_id: str = ""
    existing_team_code: str = ""
    existing_event_name: str = ""
    existing_event_code: str = ""
    existing_athlete_id: str = ""
    existing_athlete_name: str = ""
    existing_dob: str = ""


@dataclass
class IntegrityCheckResult:
    conflicts: list[IntegrityConflict] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    @property
    def codes(self) -> set[str]:
        return {conflict.code for conflict in self.conflicts}


def candidate_from_cart_item(item: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(item.get("entry_rows", []) or [])
    first = rows[0] if rows else {}
    return {
        "registration_id": _clean(item.get("registration_id")),
        "competition_id": _clean(first.get("competition_id")),
        "organization_id": _clean(first.get("organization_id")),
        "team_code": _clean(first.get("team_code")),
        "athlete_id": _clean(first.get("unique_id")),
        "athlete_name": _clean(item.get("athlete_name") or first.get("full_name")),
        "dob": _clean(first.get("birth_date")),
        "events": [
            {
                "event_name": _clean(row.get("event")),
                "event_code": _clean(row.get("event_code")),
            }
            for row in rows
        ],
    }


def _same_person(candidate: Mapping[str, Any], existing: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return (same_person, identity_collision)."""
    candidate_id = _norm(candidate.get("athlete_id"))
    existing_id = _norm(existing.get("ATHLETE_ID") or existing.get("athlete_id"))

    candidate_name = normalise_person_name(candidate.get("athlete_name"))
    existing_name = normalise_person_name(
        existing.get("ATHLETE_NAME") or existing.get("athlete_name")
    )
    candidate_dob = _normalise_dob(candidate.get("dob"))
    existing_dob = _normalise_dob(existing.get("DOB") or existing.get("dob"))

    same_name_dob = bool(
        candidate_name
        and existing_name
        and candidate_dob
        and existing_dob
        and candidate_name == existing_name
        and candidate_dob == existing_dob
    )

    if candidate_id and existing_id and candidate_id == existing_id:
        return True, False

    # Two different explicit IDs for the same exact name+DOB are not silently
    # treated as the same person. They require SA Events review.
    if same_name_dob and candidate_id and existing_id and candidate_id != existing_id:
        return False, True

    # Fallback identity when either side lacks an athlete ID.
    if same_name_dob and (not candidate_id or not existing_id):
        return True, False

    return False, False


def check_candidate_against_existing(
    candidate: Mapping[str, Any],
    existing_entries: Iterable[Mapping[str, Any]],
) -> IntegrityCheckResult:
    result = IntegrityCheckResult()

    competition_id = _norm(candidate.get("competition_id"))
    candidate_team = _normalise_team(candidate)
    candidate_events = {
        _normalise_event(event)
        for event in (candidate.get("events", []) or [])
        if _normalise_event(event)
    }

    seen_conflict_keys: set[tuple[str, str, str]] = set()

    for existing in existing_entries:
        if not is_active_existing_entry(existing):
            continue
        if _norm(existing.get("COMPETITION_ID") or existing.get("competition_id")) != competition_id:
            continue

        same_person, identity_collision = _same_person(candidate, existing)

        if identity_collision:
            key = (
                "IDENTITY_COLLISION",
                _clean(existing.get("ENTRY_ID")),
                _clean(existing.get("ATHLETE_ID")),
            )
            if key not in seen_conflict_keys:
                seen_conflict_keys.add(key)
                result.conflicts.append(
                    IntegrityConflict(
                        code="IDENTITY_COLLISION",
                        message=(
                            "An existing athlete has the same name and date of birth "
                            "but a different athlete ID. SA Events must review the "
                            "identity before another entry is submitted."
                        ),
                        existing_entry_id=_clean(existing.get("ENTRY_ID")),
                        existing_order_id=_clean(existing.get("ORDER_ID")),
                        existing_registration_id=_clean(existing.get("REGISTRATION_ID")),
                        existing_organization_id=_clean(existing.get("ORGANIZATION_ID")),
                        existing_team_code=_clean(existing.get("TEAM_CODE")),
                        existing_event_name=_clean(existing.get("EVENT_NAME")),
                        existing_event_code=_clean(existing.get("EVENT_CODE")),
                        existing_athlete_id=_clean(existing.get("ATHLETE_ID")),
                        existing_athlete_name=_clean(existing.get("ATHLETE_NAME")),
                        existing_dob=_clean(existing.get("DOB")),
                    )
                )
            continue

        if not same_person:
            continue

        existing_team = _normalise_team(existing)
        if candidate_team and existing_team and candidate_team != existing_team:
            # One conflicting team/organisation should produce one conflict,
            # even when that athlete already has several active event entries
            # for the same team. This keeps the user-facing warning and audit
            # trail concise while still blocking the registration.
            key = (
                "TEAM_CONFLICT",
                existing_team,
                "",
            )
            if key not in seen_conflict_keys:
                seen_conflict_keys.add(key)
                result.conflicts.append(
                    IntegrityConflict(
                        code="TEAM_CONFLICT",
                        message=(
                            "This athlete already has an active entry for this "
                            "competition under a different team/organisation. "
                            "An athlete may represent only one team in the same competition."
                        ),
                        existing_entry_id=_clean(existing.get("ENTRY_ID")),
                        existing_order_id=_clean(existing.get("ORDER_ID")),
                        existing_registration_id=_clean(existing.get("REGISTRATION_ID")),
                        existing_organization_id=_clean(existing.get("ORGANIZATION_ID")),
                        existing_team_code=_clean(existing.get("TEAM_CODE")),
                        existing_event_name=_clean(existing.get("EVENT_NAME")),
                        existing_event_code=_clean(existing.get("EVENT_CODE")),
                        existing_athlete_id=_clean(existing.get("ATHLETE_ID")),
                        existing_athlete_name=_clean(existing.get("ATHLETE_NAME")),
                        existing_dob=_clean(existing.get("DOB")),
                    )
                )
            # A team conflict is sufficient to block the candidate. Continue so
            # the user can see all relevant existing records, but do not also
            # report duplicate-event noise for the same row.
            continue

        existing_event = _normalise_event(existing)
        if existing_event and existing_event in candidate_events:
            key = (
                "DUPLICATE_EVENT",
                _clean(existing.get("ENTRY_ID")),
                existing_event,
            )
            if key not in seen_conflict_keys:
                seen_conflict_keys.add(key)
                event_label = _clean(existing.get("EVENT_NAME")) or _clean(existing.get("EVENT_CODE"))
                result.conflicts.append(
                    IntegrityConflict(
                        code="DUPLICATE_EVENT",
                        message=(
                            f"This athlete already has an active {event_label or 'event'} "
                            "entry in this competition. The same athlete/event cannot "
                            "be registered twice."
                        ),
                        existing_entry_id=_clean(existing.get("ENTRY_ID")),
                        existing_order_id=_clean(existing.get("ORDER_ID")),
                        existing_registration_id=_clean(existing.get("REGISTRATION_ID")),
                        existing_organization_id=_clean(existing.get("ORGANIZATION_ID")),
                        existing_team_code=_clean(existing.get("TEAM_CODE")),
                        existing_event_name=_clean(existing.get("EVENT_NAME")),
                        existing_event_code=_clean(existing.get("EVENT_CODE")),
                        existing_athlete_id=_clean(existing.get("ATHLETE_ID")),
                        existing_athlete_name=_clean(existing.get("ATHLETE_NAME")),
                        existing_dob=_clean(existing.get("DOB")),
                    )
                )

    return result
