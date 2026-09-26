"""Live registration data-quality checks for SAA Admin review.

Phase 5D is deliberately conservative: this module *flags* suspicious data but
never merges athlete identities or deletes registrations automatically.  SA
Events Admin remains the decision maker and uses the existing audited amendment
and withdrawal controls to correct records.

The scan works entirely from the current transaction-sheet read model so it is
safe to unit test and does not perform Google Sheets I/O itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from typing import Any, Iterable, Mapping

from signup.athlete_integrity import is_active_existing_entry, normalise_person_name


_TRUE_VALUES = {"1", "true", "yes", "y"}
_INACTIVE_REGISTRATION_STATUSES = {
    "WITHDRAWN",
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "VOID",
}
_REVIEW_ACTIONS = {
    "DATA_QUALITY_REVIEW_ACKNOWLEDGED": "ACKNOWLEDGED",
    "DATA_QUALITY_FALSE_POSITIVE": "FALSE_POSITIVE",
}


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    return _clean(value).casefold()


def _normalise_dob(value: Any) -> str:
    text = _clean(value)
    return text[:10] if text else ""


def _normalise_event(row: Mapping[str, Any]) -> str:
    name = _norm(row.get("EVENT_NAME") or row.get("event_name") or row.get("event"))
    if name:
        return f"name:{name}"
    code = _norm(row.get("EVENT_CODE") or row.get("event_code"))
    return f"code:{code}" if code else ""


def _canonical_field_value(field: str, value: Any) -> str:
    field = _clean(field).upper()
    if field == "DOB":
        return _normalise_dob(value)
    if field == "ATHLETE_NAME":
        return normalise_person_name(value)
    if field == "GENDER":
        gender = _norm(value)
        if gender in {"m", "male"}:
            return "male"
        if gender in {"f", "female"}:
            return "female"
        return gender
    return _norm(value)


def _is_deleted(row: Mapping[str, Any]) -> bool:
    return _norm(row.get("IS_DELETED") or row.get("is_deleted")) in _TRUE_VALUES


def _is_active_registration(row: Mapping[str, Any]) -> bool:
    if _is_deleted(row):
        return False
    return _clean(row.get("STATUS") or row.get("status")).upper() not in _INACTIVE_REGISTRATION_STATUSES


def _person_key(row: Mapping[str, Any]) -> str:
    athlete_id = _norm(row.get("ATHLETE_ID") or row.get("athlete_id"))
    if athlete_id:
        return f"id:{athlete_id}"
    name = normalise_person_name(row.get("ATHLETE_NAME") or row.get("athlete_name"))
    dob = _normalise_dob(row.get("DOB") or row.get("dob"))
    if name and dob:
        return f"name_dob:{name}|{dob}"
    return ""


def _team_key(row: Mapping[str, Any]) -> str:
    org = _norm(row.get("ORGANIZATION_ID") or row.get("organization_id"))
    if org:
        return f"org:{org}"
    team = _norm(row.get("TEAM_CODE") or row.get("team_code"))
    return f"team:{team}" if team else ""


def _stable_issue_key(issue_type: str, *parts: Any) -> str:
    payload = "|".join([issue_type] + [_clean(part) for part in parts])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20].upper()
    return f"DQ-{digest}"


def _sorted_unique(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({_clean(value) for value in values if _clean(value)}))


@dataclass(frozen=True)
class DataQualityIssue:
    issue_key: str
    issue_type: str
    severity: str
    summary: str
    details: str
    recommended_action: str
    competition_id: str = ""
    athlete_name: str = ""
    dob: str = ""
    athlete_ids: tuple[str, ...] = ()
    organization_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    registration_ids: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        return {
            "ISSUE_KEY": self.issue_key,
            "SEVERITY": self.severity,
            "ISSUE_TYPE": self.issue_type,
            "COMPETITION_ID": self.competition_id,
            "ATHLETE_NAME": self.athlete_name,
            "DOB": self.dob,
            "ATHLETE_IDS": ", ".join(self.athlete_ids),
            "ORGANIZATION_IDS": ", ".join(self.organization_ids),
            "ORDER_IDS": ", ".join(self.order_ids),
            "REGISTRATION_IDS": ", ".join(self.registration_ids),
            "ENTRY_IDS": ", ".join(self.entry_ids),
            "SUMMARY": self.summary,
            "DETAILS": self.details,
            "RECOMMENDED_ACTION": self.recommended_action,
        }


def _issue(
    *,
    issue_type: str,
    severity: str,
    summary: str,
    details: str,
    recommended_action: str,
    key_parts: Iterable[Any],
    rows: Iterable[Mapping[str, Any]] = (),
    competition_id: str = "",
    athlete_name: str = "",
    dob: str = "",
) -> DataQualityIssue:
    row_list = list(rows)
    if not competition_id:
        competition_ids = _sorted_unique(row.get("COMPETITION_ID") for row in row_list)
        competition_id = competition_ids[0] if len(competition_ids) == 1 else ", ".join(competition_ids)
    if not athlete_name:
        athlete_name = next((_clean(row.get("ATHLETE_NAME")) for row in row_list if _clean(row.get("ATHLETE_NAME"))), "")
    if not dob:
        dob = next((_normalise_dob(row.get("DOB")) for row in row_list if _normalise_dob(row.get("DOB"))), "")
    return DataQualityIssue(
        issue_key=_stable_issue_key(issue_type, *key_parts),
        issue_type=issue_type,
        severity=severity,
        summary=summary,
        details=details,
        recommended_action=recommended_action,
        competition_id=competition_id,
        athlete_name=athlete_name,
        dob=dob,
        athlete_ids=_sorted_unique(row.get("ATHLETE_ID") for row in row_list),
        organization_ids=_sorted_unique(row.get("ORGANIZATION_ID") for row in row_list),
        order_ids=_sorted_unique(row.get("ORDER_ID") for row in row_list),
        registration_ids=_sorted_unique(row.get("REGISTRATION_ID") for row in row_list),
        entry_ids=_sorted_unique(row.get("ENTRY_ID") for row in row_list),
    )


def _valid_iso_date(value: Any) -> bool:
    text = _normalise_dob(value)
    if not text:
        return False
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def scan_data_quality(
    *,
    orders: Iterable[Mapping[str, Any]],
    registrations: Iterable[Mapping[str, Any]],
    entries: Iterable[Mapping[str, Any]],
) -> list[DataQualityIssue]:
    """Return deterministic, de-duplicated live data-quality issues.

    The scan intentionally focuses on registration identity and referential
    integrity. Financial reconciliation remains in the finance/refund modules,
    while Hy-Tek result matching belongs to Phase 6.
    """
    orders = [dict(row) for row in orders]
    registrations = [dict(row) for row in registrations]
    entries = [dict(row) for row in entries]

    order_by_id = {_clean(row.get("ORDER_ID")): row for row in orders if _clean(row.get("ORDER_ID"))}
    registration_by_id = {
        _clean(row.get("REGISTRATION_ID")): row
        for row in registrations
        if _clean(row.get("REGISTRATION_ID"))
    }
    active_regs = [row for row in registrations if _is_active_registration(row)]
    active_entries = [row for row in entries if is_active_existing_entry(row)]

    issues: list[DataQualityIssue] = []

    # 1) Same exact name + DOB but different explicit athlete IDs.  This mirrors
    # the Phase 5A registration-time collision rule and catches historical or
    # manually edited data for SA Events review.
    by_name_dob: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in active_regs:
        name_key = normalise_person_name(row.get("ATHLETE_NAME"))
        dob_key = _normalise_dob(row.get("DOB"))
        if name_key and dob_key:
            by_name_dob.setdefault((name_key, dob_key), []).append(row)
    for (name_key, dob_key), rows in by_name_dob.items():
        ids = _sorted_unique(row.get("ATHLETE_ID") for row in rows)
        if len(ids) <= 1:
            continue
        issues.append(
            _issue(
                issue_type="IDENTITY_COLLISION",
                severity="CRITICAL",
                summary="Same name and DOB are attached to different athlete IDs.",
                details=f"Athlete IDs: {', '.join(ids)}.",
                recommended_action=(
                    "SA Events should compare the listed registrations, decide which record is correct, "
                    "and amend/withdraw the duplicate using the audited Admin Operations controls. "
                    "Do not auto-merge identities."
                ),
                key_parts=(name_key, dob_key, *ids),
                rows=rows,
            )
        )

    # 2) The inverse corruption: one athlete ID points to conflicting name/DOB
    # pairs.  This is stronger than a spelling-only warning and should be
    # reviewed before export/results reconciliation.
    by_athlete_id: dict[str, list[dict[str, Any]]] = {}
    for row in active_regs:
        athlete_id = _clean(row.get("ATHLETE_ID"))
        if athlete_id:
            by_athlete_id.setdefault(athlete_id.casefold(), []).append(row)
    for athlete_id_key, rows in by_athlete_id.items():
        signatures = {
            (normalise_person_name(row.get("ATHLETE_NAME")), _normalise_dob(row.get("DOB")))
            for row in rows
            if normalise_person_name(row.get("ATHLETE_NAME")) or _normalise_dob(row.get("DOB"))
        }
        if len(signatures) <= 1:
            continue
        issues.append(
            _issue(
                issue_type="ATHLETE_ID_CONFLICT",
                severity="CRITICAL",
                summary="One athlete ID is attached to conflicting name/DOB records.",
                details=f"Athlete ID: {_clean(rows[0].get('ATHLETE_ID'))}.",
                recommended_action=(
                    "Review the underlying registrations and correct the erroneous identity fields. "
                    "If a duplicate registration is invalid, withdraw it rather than silently merging records."
                ),
                key_parts=(athlete_id_key, json.dumps(sorted(signatures))),
                rows=rows,
            )
        )

    # 3) Duplicate active athlete/event entries.
    duplicate_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in active_entries:
        comp = _norm(row.get("COMPETITION_ID"))
        person = _person_key(row)
        event = _normalise_event(row)
        if comp and person and event:
            duplicate_groups.setdefault((comp, person, event), []).append(row)
    for (comp, person, event), rows in duplicate_groups.items():
        if len(rows) <= 1:
            continue
        issues.append(
            _issue(
                issue_type="DUPLICATE_ACTIVE_EVENT",
                severity="CRITICAL",
                summary="Athlete has more than one active entry for the same event in one competition.",
                details=f"{len(rows)} active entries were found.",
                recommended_action=(
                    "Confirm the legitimate entry and withdraw the duplicate entry/entries with a reason. "
                    "The withdrawal audit trail preserves the historical record."
                ),
                key_parts=(comp, person, event, *_sorted_unique(row.get("ENTRY_ID") for row in rows)),
                rows=rows,
            )
        )

    # 4) Same athlete actively represented by multiple organisations in one competition.
    team_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in active_entries:
        comp = _norm(row.get("COMPETITION_ID"))
        person = _person_key(row)
        if comp and person:
            team_groups.setdefault((comp, person), []).append(row)
    for (comp, person), rows in team_groups.items():
        team_keys = {_team_key(row) for row in rows if _team_key(row)}
        if len(team_keys) <= 1:
            continue
        issues.append(
            _issue(
                issue_type="TEAM_CONFLICT",
                severity="CRITICAL",
                summary="Athlete has active entries under multiple teams/organisations in one competition.",
                details=f"Teams/organisations: {', '.join(sorted(team_keys))}.",
                recommended_action=(
                    "SA Events should determine the correct team, amend the valid registration if needed, "
                    "and withdraw the conflicting duplicate entry/entries."
                ),
                key_parts=(comp, person, *sorted(team_keys)),
                rows=rows,
            )
        )

    # 5) Orphan registrations and event entries.
    for row in registrations:
        registration_id = _clean(row.get("REGISTRATION_ID"))
        order_id = _clean(row.get("ORDER_ID"))
        if order_id and order_id not in order_by_id:
            issues.append(
                _issue(
                    issue_type="ORPHAN_REGISTRATION",
                    severity="ERROR",
                    summary="Registration references an order that does not exist.",
                    details=f"Missing ORDER_ID: {order_id}.",
                    recommended_action="Review the transaction sheets before editing or exporting this registration.",
                    key_parts=(registration_id, order_id),
                    rows=(row,),
                )
            )
    for row in entries:
        entry_id = _clean(row.get("ENTRY_ID"))
        registration_id = _clean(row.get("REGISTRATION_ID"))
        order_id = _clean(row.get("ORDER_ID"))
        missing = []
        if registration_id and registration_id not in registration_by_id:
            missing.append(f"REGISTRATION_ID {registration_id}")
        if order_id and order_id not in order_by_id:
            missing.append(f"ORDER_ID {order_id}")
        if missing:
            issues.append(
                _issue(
                    issue_type="ORPHAN_EVENT_ENTRY",
                    severity="ERROR",
                    summary="Event entry has a broken parent reference.",
                    details="Missing: " + "; ".join(missing) + ".",
                    recommended_action="Review the transaction-sheet relationship before amending, billing or exporting this entry.",
                    key_parts=(entry_id, *missing),
                    rows=(row,),
                )
            )

    # 6) Entry-vs-registration projection mismatches.  These fields should agree
    # because admin amendments update athlete-level fields across the full registration.
    comparable_fields = (
        "COMPETITION_ID",
        "ATHLETE_ID",
        "ORGANIZATION_ID",
        "ATHLETE_NAME",
        "DOB",
        "GENDER",
        "TEAM_CODE",
        "TEAM_NAME",
    )
    for row in entries:
        registration_id = _clean(row.get("REGISTRATION_ID"))
        parent = registration_by_id.get(registration_id)
        if not parent:
            continue
        mismatches = []
        for field in comparable_fields:
            left = _clean(parent.get(field))
            right = _clean(row.get(field))
            if (
                left
                and right
                and _canonical_field_value(field, left) != _canonical_field_value(field, right)
            ):
                mismatches.append(f"{field}: registration={left!r}, entry={right!r}")
        if mismatches:
            entry_id = _clean(row.get("ENTRY_ID"))
            issues.append(
                _issue(
                    issue_type="ENTRY_REGISTRATION_MISMATCH",
                    severity="WARNING",
                    summary="Event entry identity/team fields disagree with its registration.",
                    details="; ".join(mismatches),
                    recommended_action=(
                        "Review the registration and entry together, then use Admin Operations to make the intended "
                        "athlete/team values consistent."
                    ),
                    key_parts=(entry_id, *mismatches),
                    rows=(parent, row),
                )
            )

    # 7) Missing/invalid critical fields.  ATHLETE_ID is intentionally not
    # mandatory here because some historical/external records may legitimately
    # lack it; Phase 5A already handles identity conservatively.
    for row in active_regs:
        registration_id = _clean(row.get("REGISTRATION_ID"))
        missing = [
            field for field in ("COMPETITION_ID", "ORGANIZATION_ID", "ATHLETE_NAME", "DOB", "GENDER")
            if not _clean(row.get(field))
        ]
        if _clean(row.get("DOB")) and not _valid_iso_date(row.get("DOB")):
            missing.append("DOB_INVALID")
        if missing:
            issues.append(
                _issue(
                    issue_type="MISSING_REGISTRATION_DATA",
                    severity="WARNING",
                    summary="Active registration is missing or has invalid critical identity data.",
                    details="Fields: " + ", ".join(missing) + ".",
                    recommended_action="Complete/correct the registration data before downstream export or reconciliation.",
                    key_parts=(registration_id, *missing),
                    rows=(row,),
                )
            )
    for row in active_entries:
        entry_id = _clean(row.get("ENTRY_ID"))
        missing = [
            field for field in ("COMPETITION_ID", "ORGANIZATION_ID", "ATHLETE_NAME", "DOB", "GENDER", "EVENT_NAME", "DIVISION")
            if not _clean(row.get(field))
        ]
        if _clean(row.get("DOB")) and not _valid_iso_date(row.get("DOB")):
            missing.append("DOB_INVALID")
        if missing:
            issues.append(
                _issue(
                    issue_type="MISSING_ENTRY_DATA",
                    severity="WARNING",
                    summary="Active event entry is missing or has invalid critical data.",
                    details="Fields: " + ", ".join(missing) + ".",
                    recommended_action="Complete/correct the entry before billing, export or results reconciliation.",
                    key_parts=(entry_id, *missing),
                    rows=(row,),
                )
            )

    # De-duplicate by deterministic key and return severe issues first.
    unique = {issue.issue_key: issue for issue in issues}
    severity_order = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}
    return sorted(
        unique.values(),
        key=lambda issue: (
            severity_order.get(issue.severity, 9),
            issue.issue_type,
            issue.competition_id,
            issue.athlete_name.casefold(),
            issue.issue_key,
        ),
    )


def latest_review_state(audit_rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Return the latest Phase 5D review decision for each deterministic issue key."""
    candidates: dict[str, dict[str, str]] = {}
    for row in audit_rows:
        if _clean(row.get("ENTITY_TYPE")).upper() != "DATA_QUALITY_ISSUE":
            continue
        action = _clean(row.get("ACTION")).upper()
        status = _REVIEW_ACTIONS.get(action)
        if not status:
            continue
        issue_key = _clean(row.get("ENTITY_ID"))
        if not issue_key:
            continue
        stamp = _clean(row.get("TIMESTAMP"))
        previous = candidates.get(issue_key)
        if previous and _clean(previous.get("TIMESTAMP")) > stamp:
            continue
        candidates[issue_key] = {
            "STATUS": status,
            "ACTION": action,
            "TIMESTAMP": stamp,
            "USER_ID": _clean(row.get("USER_ID")),
            "USER_EMAIL": _clean(row.get("USER_EMAIL")),
            "REASON": _clean(row.get("REASON")),
        }
    return candidates
