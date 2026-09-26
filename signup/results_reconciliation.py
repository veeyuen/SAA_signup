"""Phase 6C result-file validation and registration reconciliation.

The result file entering this module is the canonical post-notebook output, not
raw Hy-Tek Meet Manager data.  The current canonical schema is the historic
40-column BigQuery result schema plus the boolean INDOOR column.

Automatic athlete reconciliation is deliberately conservative.  The written
requirements say Unique ID + DOB + name must all match.  Name variations and
same-name cases therefore remain review items rather than fuzzy auto-matches.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import re
import unicodedata
from typing import Any, Iterable, Mapping

import pandas as pd


# The current notebook output contains the first 40 fields. INDOOR is the only
# production-schema addition communicated for Phase 6C. Column order is not
# enforced on upload; this tuple supplies the canonical set and deterministic
# fingerprint order.
CANONICAL_RESULT_COLUMNS: tuple[str, ...] = (
    "FIRST_NAME",
    "LAST_NAME",
    "OTHER_NAME",
    "NAME",
    "RANK",
    "TAG_ID",
    "TEAM",
    "SEED",
    "RESULT",
    "QUALIFICATION",
    "HEAT",
    "LANE",
    "WIND",
    "EVENT",
    "DIVISION",
    "STAGE",
    "POINTS",
    "AGE",
    "GENDER",
    "UNIQUE_ID",
    "NATIONALITY",
    "DICT_RESULTS",
    "YEAR",
    "DATE",
    "COMPETITION",
    "REGION",
    "DOB",
    "GROUP",
    "CATEGORY_EVENT",
    "ATHLETE_ID",
    "SOURCE",
    "REMARKS",
    "TIMESTAMP",
    "VENUE",
    "SUB_EVENT",
    "SESSION",
    "EVENT_CLASS",
    "DISTANCE",
    "HOST_CITY",
    "RX_TIME",
    "INDOOR",
)

LEGACY_RESULT_COLUMNS_40: tuple[str, ...] = CANONICAL_RESULT_COLUMNS[:-1]


class ResultsSchemaError(ValueError):
    """Raised when an uploaded results file is not safe to reconcile."""


@dataclass(frozen=True)
class ResultsSchemaValidation:
    row_count: int
    required_column_count: int
    extra_columns: tuple[str, ...]
    used_legacy_indoor_default: bool
    indoor_value_if_defaulted: bool | None


@dataclass(frozen=True)
class ReconciliationBundle:
    rows: pd.DataFrame

    @property
    def matched_count(self) -> int:
        return int((self.rows["MATCH_STATUS"] == "MATCHED").sum()) if not self.rows.empty else 0

    @property
    def review_count(self) -> int:
        return int((self.rows["MATCH_STATUS"] == "REVIEW").sum()) if not self.rows.empty else 0

    @property
    def unmatched_count(self) -> int:
        return int((self.rows["MATCH_STATUS"] == "UNMATCHED").sum()) if not self.rows.empty else 0


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return str(value).strip() == ""


def _clean(value: Any) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def _collapse_space(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value))
    return re.sub(r"\s+", " ", text).strip()


def normalise_exact_name(value: Any) -> str:
    """Conservative name key: case/Unicode/whitespace only.

    Punctuation, token order, hyphenation and actual spelling are intentionally
    preserved. Those are name variations and must not be silently auto-matched.
    """
    return _collapse_space(value).casefold()


def _normalise_simple(value: Any) -> str:
    return _collapse_space(value).casefold()


def _normalise_uid(value: Any) -> str:
    return _collapse_space(value).casefold()


def normalise_dob(value: Any) -> str:
    """Return DOB as YYYY-MM-DD, or blank if the supplied value is invalid."""
    if _is_missing(value):
        return ""

    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()

    raw = _clean(value)
    # Explicit formats first to avoid ambiguous day/month interpretation.
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        candidate = raw[:19] if "%H" in fmt and len(raw) >= 19 else raw
        try:
            return dt.datetime.strptime(candidate, fmt).date().isoformat()
        except (TypeError, ValueError):
            pass

    parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, float) and value in {0.0, 1.0}:
        return bool(int(value))

    raw = _clean(value).casefold()
    if raw in {"true", "t", "1", "yes", "y"}:
        return True
    if raw in {"false", "f", "0", "no", "n"}:
        return False
    raise ResultsSchemaError(f"INDOOR contains a non-boolean value: {value!r}.")


def validate_canonical_results(
    results: pd.DataFrame,
    *,
    indoor_default: bool | None = None,
) -> tuple[pd.DataFrame, ResultsSchemaValidation]:
    """Validate and normalize the canonical results upload.

    The required schema is the existing 40-column notebook output plus INDOOR.
    A 40-column legacy notebook file is accepted only when the caller explicitly
    supplies ``indoor_default``.  No indoor/outdoor value is guessed.

    Extra columns are preserved and reported but do not block reconciliation.
    Required-column order is not enforced.
    """
    if results is None:
        raise ResultsSchemaError("No results dataframe was supplied.")

    df = results.copy()
    normalized_columns = [str(column).strip() for column in df.columns]
    if len(set(normalized_columns)) != len(normalized_columns):
        duplicates = sorted(
            {column for column in normalized_columns if normalized_columns.count(column) > 1}
        )
        raise ResultsSchemaError(
            "Duplicate column names were found after trimming whitespace: "
            + ", ".join(duplicates)
        )
    df.columns = normalized_columns

    required = set(CANONICAL_RESULT_COLUMNS)
    present = set(df.columns)
    missing = sorted(required - present)
    used_legacy_default = False

    if missing == ["INDOOR"] and indoor_default is not None:
        df["INDOOR"] = bool(indoor_default)
        present.add("INDOOR")
        missing = []
        used_legacy_default = True

    if missing:
        message = "Missing required canonical result columns: " + ", ".join(missing)
        if missing == ["INDOOR"]:
            message += (
                ". This is the legacy 40-column notebook schema. Supply an explicit "
                "indoor/outdoor value for the file or update the notebook to emit INDOOR."
            )
        raise ResultsSchemaError(message)

    # A canonical 41-column file must contain a real boolean for every row.
    indoor_values: list[bool] = []
    for row_number, value in enumerate(df["INDOOR"].tolist(), start=2):
        if _is_missing(value):
            raise ResultsSchemaError(
                f"INDOOR is blank at source row {row_number}; every result row must be True or False."
            )
        try:
            indoor_values.append(_coerce_bool(value))
        except ResultsSchemaError as exc:
            raise ResultsSchemaError(f"Source row {row_number}: {exc}") from exc
    df["INDOOR"] = indoor_values

    extras = tuple(sorted(set(df.columns) - required))
    validation = ResultsSchemaValidation(
        row_count=len(df),
        required_column_count=len(CANONICAL_RESULT_COLUMNS),
        extra_columns=extras,
        used_legacy_indoor_default=used_legacy_default,
        indoor_value_if_defaulted=(bool(indoor_default) if used_legacy_default else None),
    )
    return df, validation


def _truthy(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _active_confirmed(row: Mapping[str, Any]) -> bool:
    return _clean(row.get("STATUS")).upper() == "CONFIRMED" and not _truthy(
        row.get("IS_DELETED")
    )


def _registration_name(row: Mapping[str, Any]) -> str:
    # ATHLETE_NAME is the registration's canonical/display name and is the first
    # value to test. Structured fields are only a fallback when it is blank.
    athlete_name = _collapse_space(row.get("ATHLETE_NAME"))
    if athlete_name:
        return athlete_name
    return " ".join(
        part
        for part in (
            _collapse_space(row.get("FIRST_NAME")),
            _collapse_space(row.get("OTHER_NAME")),
            _collapse_space(row.get("LAST_NAME")),
        )
        if part
    ).strip()


def _result_fingerprint(row: Mapping[str, Any]) -> str:
    values = []
    for column in CANONICAL_RESULT_COLUMNS:
        value = row.get(column, "")
        if column == "INDOOR" and isinstance(value, bool):
            text = "TRUE" if value else "FALSE"
        else:
            text = _collapse_space(value)
        values.append(text)
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest().upper()
    return digest


def _ids(rows: Iterable[Mapping[str, Any]], field: str) -> str:
    values = sorted({_clean(row.get(field)) for row in rows if _clean(row.get(field))})
    return ", ".join(values)


def _candidate_dobs(rows: Iterable[Mapping[str, Any]]) -> str:
    values = sorted({normalise_dob(row.get("DOB")) for row in rows if normalise_dob(row.get("DOB"))})
    return ", ".join(values)


def _candidate_names(rows: Iterable[Mapping[str, Any]]) -> str:
    values = sorted({_registration_name(row) for row in rows if _registration_name(row)})
    return " | ".join(values)


def _base_match_fields(result: Mapping[str, Any], row_number: int) -> dict[str, Any]:
    return {
        "RESULT_ROW_NUMBER": row_number,
        "RESULT_FINGERPRINT": _result_fingerprint(result),
        "MATCH_STATUS": "",
        "MATCH_REASON": "",
        "MATCH_DETAILS": "",
        "REGISTRATION_ID": "",
        "ENTRY_ID": "",
        "ORDER_ID": "",
        "REGISTRATION_ATHLETE_ID": "",
        "REGISTRATION_ATHLETE_NAME": "",
        "REGISTRATION_DOB": "",
        "REGISTERED_EVENT": "",
        "REGISTERED_DIVISION": "",
    }


def _finalize(
    fields: dict[str, Any],
    *,
    status: str,
    reason: str,
    details: str,
    registration: Mapping[str, Any] | None = None,
    entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fields["MATCH_STATUS"] = status
    fields["MATCH_REASON"] = reason
    fields["MATCH_DETAILS"] = details
    if registration is not None:
        fields["REGISTRATION_ID"] = _clean(registration.get("REGISTRATION_ID"))
        fields["ORDER_ID"] = _clean(registration.get("ORDER_ID"))
        fields["REGISTRATION_ATHLETE_ID"] = _clean(registration.get("ATHLETE_ID"))
        fields["REGISTRATION_ATHLETE_NAME"] = _registration_name(registration)
        fields["REGISTRATION_DOB"] = normalise_dob(registration.get("DOB"))
    if entry is not None:
        fields["ENTRY_ID"] = _clean(entry.get("ENTRY_ID"))
        fields["ORDER_ID"] = _clean(entry.get("ORDER_ID")) or fields["ORDER_ID"]
        fields["REGISTERED_EVENT"] = _clean(entry.get("EVENT_NAME"))
        fields["REGISTERED_DIVISION"] = _clean(entry.get("DIVISION"))
    return fields


def reconcile_results_to_registrations(
    results: pd.DataFrame,
    registrations: Iterable[Mapping[str, Any]],
    event_entries: Iterable[Mapping[str, Any]],
    *,
    competition_id: str,
    competition_name: str,
) -> ReconciliationBundle:
    """Reconcile canonical result rows to one competition's registrations.

    Matching rule:
      1. selected competition matches the result COMPETITION name;
      2. result UNIQUE_ID == registration ATHLETE_ID;
      3. result DOB == registration DOB;
      4. result NAME == registration ATHLETE_NAME (conservative exact-normalized name);
      5. result EVENT has one active confirmed EVENT_ENTRIES row for that registration.

    Steps 2-4 implement the written requirement that all three identity fields
    must match.  There is no fuzzy/token-order/punctuation name auto-match.
    """
    if not competition_id:
        raise ValueError("competition_id is required.")
    if not competition_name:
        raise ValueError("competition_name is required.")

    # Validate the expected 41-column form before matching. This call does not
    # accept a legacy default because the page performs that explicit user step.
    canonical, _ = validate_canonical_results(results)

    registration_rows = [
        dict(row)
        for row in registrations
        if _clean(row.get("COMPETITION_ID")) == _clean(competition_id)
    ]
    entry_rows = [
        dict(row)
        for row in event_entries
        if _clean(row.get("COMPETITION_ID")) == _clean(competition_id)
    ]

    active_regs = [row for row in registration_rows if _active_confirmed(row)]
    active_entries = [row for row in entry_rows if _active_confirmed(row)]

    all_regs_by_uid: dict[str, list[dict[str, Any]]] = {}
    active_regs_by_uid: dict[str, list[dict[str, Any]]] = {}
    for row in registration_rows:
        uid = _normalise_uid(row.get("ATHLETE_ID"))
        if uid:
            all_regs_by_uid.setdefault(uid, []).append(row)
    for row in active_regs:
        uid = _normalise_uid(row.get("ATHLETE_ID"))
        if uid:
            active_regs_by_uid.setdefault(uid, []).append(row)

    all_entries_by_reg: dict[str, list[dict[str, Any]]] = {}
    active_entries_by_reg: dict[str, list[dict[str, Any]]] = {}
    for row in entry_rows:
        reg_id = _clean(row.get("REGISTRATION_ID"))
        if reg_id:
            all_entries_by_reg.setdefault(reg_id, []).append(row)
    for row in active_entries:
        reg_id = _clean(row.get("REGISTRATION_ID"))
        if reg_id:
            active_entries_by_reg.setdefault(reg_id, []).append(row)

    expected_competition_key = _normalise_simple(competition_name)
    output_rows: list[dict[str, Any]] = []

    # itertuples is avoided because canonical column names may evolve; dict rows
    # make the output/persistence boundary explicit.
    for offset, result in enumerate(canonical.to_dict(orient="records"), start=2):
        fields = _base_match_fields(result, offset)
        result_competition = _collapse_space(result.get("COMPETITION"))
        result_uid_raw = _collapse_space(result.get("UNIQUE_ID"))
        result_name_raw = _collapse_space(result.get("NAME"))
        result_dob_raw = _collapse_space(result.get("DOB"))
        result_event_raw = _collapse_space(result.get("EVENT"))

        if _normalise_simple(result_competition) != expected_competition_key:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="COMPETITION_MISMATCH",
                        details=(
                            f"Result competition {result_competition!r} does not match selected "
                            f"competition {competition_name!r}."
                        ),
                    ),
                }
            )
            continue

        missing_identity = [
            field
            for field, value in (
                ("UNIQUE_ID", result_uid_raw),
                ("DOB", result_dob_raw),
                ("NAME", result_name_raw),
            )
            if not value
        ]
        if missing_identity:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="MISSING_IDENTITY_FIELDS",
                        details="Missing required identity field(s): " + ", ".join(missing_identity) + ".",
                    ),
                }
            )
            continue

        result_dob = normalise_dob(result_dob_raw)
        if not result_dob:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="INVALID_DOB",
                        details=f"DOB {result_dob_raw!r} could not be parsed as a valid date.",
                    ),
                }
            )
            continue

        uid_key = _normalise_uid(result_uid_raw)
        uid_candidates = active_regs_by_uid.get(uid_key, [])
        if not uid_candidates:
            inactive_candidates = all_regs_by_uid.get(uid_key, [])
            if inactive_candidates:
                details = (
                    "The Unique ID exists for this competition, but no matching registration is "
                    "currently CONFIRMED and active. Registration IDs: "
                    + _ids(inactive_candidates, "REGISTRATION_ID")
                    + "."
                )
                reason = "NO_ACTIVE_REGISTRATION"
            else:
                details = "No registration for this competition has the supplied Unique ID."
                reason = "NO_REGISTRATION_FOR_UNIQUE_ID"
            output_rows.append(
                {
                    **result,
                    **_finalize(fields, status="UNMATCHED", reason=reason, details=details),
                }
            )
            continue

        dob_candidates = [
            row for row in uid_candidates if normalise_dob(row.get("DOB")) == result_dob
        ]
        if not dob_candidates:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="DOB_MISMATCH",
                        details=(
                            "Unique ID matched active registration(s), but DOB did not. "
                            f"Registered DOB(s): {_candidate_dobs(uid_candidates) or 'blank'}. "
                            f"Registration IDs: {_ids(uid_candidates, 'REGISTRATION_ID')}."
                        ),
                    ),
                }
            )
            continue

        result_name_key = normalise_exact_name(result_name_raw)
        name_candidates = [
            row
            for row in dob_candidates
            if normalise_exact_name(_registration_name(row)) == result_name_key
        ]
        if not name_candidates:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="NAME_MISMATCH",
                        details=(
                            "Unique ID and DOB matched, but the name did not match exactly after "
                            "case/Unicode/whitespace normalization. Registered name(s): "
                            f"{_candidate_names(dob_candidates) or 'blank'}. "
                            "Name variations require SA Events Admin review."
                        ),
                    ),
                }
            )
            continue

        if len(name_candidates) != 1:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="AMBIGUOUS_IDENTITY",
                        details=(
                            "More than one active registration matched Unique ID + DOB + name. "
                            f"Registration IDs: {_ids(name_candidates, 'REGISTRATION_ID')}."
                        ),
                    ),
                }
            )
            continue

        registration = name_candidates[0]
        registration_id = _clean(registration.get("REGISTRATION_ID"))
        if not result_event_raw:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="MISSING_EVENT",
                        details="The result row has no EVENT value.",
                        registration=registration,
                    ),
                }
            )
            continue

        event_key = _normalise_simple(result_event_raw)
        active_event_candidates = [
            row
            for row in active_entries_by_reg.get(registration_id, [])
            if _normalise_simple(row.get("EVENT_NAME")) == event_key
        ]

        if not active_event_candidates:
            inactive_same_event = [
                row
                for row in all_entries_by_reg.get(registration_id, [])
                if _normalise_simple(row.get("EVENT_NAME")) == event_key
            ]
            if inactive_same_event:
                reason = "NO_ACTIVE_EVENT_ENTRY"
                details = (
                    "Identity matched, but the corresponding event entry is not CONFIRMED/active. "
                    f"Entry IDs: {_ids(inactive_same_event, 'ENTRY_ID')}."
                )
            else:
                reason = "UNREGISTERED_EVENT"
                registered = sorted(
                    {
                        _clean(row.get("EVENT_NAME"))
                        for row in active_entries_by_reg.get(registration_id, [])
                        if _clean(row.get("EVENT_NAME"))
                    }
                )
                details = (
                    "Identity matched, but no active registered event matches this result EVENT. "
                    f"Active registered event(s): {', '.join(registered) if registered else 'none'}."
                )
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason=reason,
                        details=details,
                        registration=registration,
                    ),
                }
            )
            continue

        if len(active_event_candidates) != 1:
            output_rows.append(
                {
                    **result,
                    **_finalize(
                        fields,
                        status="REVIEW",
                        reason="AMBIGUOUS_EVENT_ENTRY",
                        details=(
                            "More than one active event entry matched the reconciled athlete/event. "
                            f"Entry IDs: {_ids(active_event_candidates, 'ENTRY_ID')}."
                        ),
                        registration=registration,
                    ),
                }
            )
            continue

        entry = active_event_candidates[0]
        output_rows.append(
            {
                **result,
                **_finalize(
                    fields,
                    status="MATCHED",
                    reason="EXACT_IDENTITY_AND_EVENT_MATCH",
                    details="Unique ID, DOB, name and active registered event matched.",
                    registration=registration,
                    entry=entry,
                ),
            }
        )

    # Put reconciliation fields first for admin readability; retain every result
    # column after them, including any harmless extra columns supplied upstream.
    output = pd.DataFrame(output_rows)
    match_columns = [
        "RESULT_ROW_NUMBER",
        "RESULT_FINGERPRINT",
        "MATCH_STATUS",
        "MATCH_REASON",
        "MATCH_DETAILS",
        "REGISTRATION_ID",
        "ENTRY_ID",
        "ORDER_ID",
        "REGISTRATION_ATHLETE_ID",
        "REGISTRATION_ATHLETE_NAME",
        "REGISTRATION_DOB",
        "REGISTERED_EVENT",
        "REGISTERED_DIVISION",
    ]
    remaining = [column for column in output.columns if column not in match_columns]
    if not output.empty:
        output = output[match_columns + remaining]
    else:
        output = pd.DataFrame(columns=match_columns + list(canonical.columns))
    return ReconciliationBundle(rows=output)
