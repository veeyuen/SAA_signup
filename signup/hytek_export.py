"""Hy-Tek Meet Manager I/E export support.

Phase 6A keeps the established semicolon-delimited I and E layouts used by the
legacy/private registration site, but determines export eligibility from the
Phase 2-5 transaction tables.

The transaction tables are authoritative for whether an entry is current and
exportable.  The legacy OUTPUT sheet may be supplied only as a transitional
name-field enrichment source for historical rows created before FIRST_NAME /
OTHER_NAME / LAST_NAME were persisted in the transaction schema.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import re
from typing import Any, Iterable, Mapping

import pandas as pd


I_FIELD_COUNT = 25
E_FIELD_COUNT = 17


@dataclass(frozen=True)
class HyTekDiagnostic:
    level: str
    code: str
    entry_id: str
    message: str


@dataclass(frozen=True)
class HyTekExportBundle:
    competition_id: str
    i_text: str
    e_text: str
    exported_entry_ids: tuple[str, ...]
    excluded_entry_ids: tuple[str, ...]
    diagnostics: tuple[HyTekDiagnostic, ...]

    @property
    def exported_count(self) -> int:
        return len(self.exported_entry_ids)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_entry_ids)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _truthy(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _safe_field(value: Any) -> str:
    """Return a Hy-Tek-safe scalar without changing the fixed field layout."""
    value = _clean(value)
    value = value.replace(";", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", value).strip()


def _format_date(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""

    # Native date/datetime first.
    if isinstance(value, dt.datetime):
        return value.date().strftime("%d/%m/%Y")
    if isinstance(value, dt.date):
        return value.strftime("%d/%m/%Y")

    # The transaction store uses ISO dates; the legacy OUTPUT sheet and test
    # fixtures may already contain DD/MM/YYYY.
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
    ):
        try:
            return dt.datetime.strptime(raw[:19] if "T" in raw and "%T" not in fmt else raw, fmt).strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            pass

    try:
        parsed = pd.to_datetime(raw, dayfirst=False, errors="raise")
        return parsed.strftime("%d/%m/%Y")
    except Exception:
        return raw


def _gender_code(value: Any) -> str:
    raw = _clean(value)
    key = raw.casefold()
    if key in {"male", "m", "men", "man"}:
        return "M"
    if key in {"female", "f", "women", "woman"}:
        return "F"
    return raw[:1].upper() if raw else ""


def _format_division(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        number = float(raw)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return raw


def export_event_code(event_name: Any, stored_event_code: Any) -> str:
    """Return the established E-file event code.

    Current transaction rows normally store the final code (100, LJ, HJ, ...).
    Older OUTPUT rows sometimes stored a numeric schedule number instead, so the
    event name remains a fallback for deriving Meet Manager-style codes.
    """
    name = _clean(event_name).upper()
    stored = _clean(stored_event_code).upper()

    # Non-numeric stored codes such as LJ, HJ, 100H are already useful.
    if stored and not stored.isdigit():
        return stored

    m = re.search(r"(\d+)\s*M", name)
    if m:
        distance = m.group(1)
        if "HURD" in name:
            return f"{distance}H"
        if "WALK" in name:
            return f"{distance}W"
        return distance

    aliases = (
        ("HIGH JUMP", "HJ"),
        ("LONG JUMP", "LJ"),
        ("TRIPLE JUMP", "TJ"),
        ("POLE VAULT", "PV"),
        ("SHOT", "SP"),
        ("DISCUS", "DT"),
        ("JAVELIN", "JT"),
        ("HAMMER", "HT"),
    )
    for label, code in aliases:
        if label in name:
            return code

    relay = re.search(r"(\d+)\s*[X×]\s*(\d+)", name)
    if relay:
        return f"{relay.group(1)}X{relay.group(2)}"

    # Numeric stored code is correct for current rows such as 100/200/800.
    return stored or name


def _mapping_rows(rows: Iterable[Mapping[str, Any]] | pd.DataFrame | None) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, pd.DataFrame):
        if rows.empty:
            return []
        return [dict(row) for row in rows.to_dict(orient="records")]
    return [dict(row) for row in rows]


def _row_get(row: Mapping[str, Any] | None, *keys: str) -> str:
    if not row:
        return ""
    by_upper = {_upper(k): v for k, v in row.items()}
    for key in keys:
        value = _clean(by_upper.get(_upper(key), ""))
        if value:
            return value
    return ""


def _legacy_indexes(rows: Iterable[Mapping[str, Any]] | pd.DataFrame | None):
    by_entry: dict[str, dict[str, Any]] = {}
    by_registration: dict[str, list[dict[str, Any]]] = {}
    for row in _mapping_rows(rows):
        entry_id = _row_get(row, "ENTRY_ID", "entry_id")
        registration_id = _row_get(row, "REGISTRATION_ID", "registration_id")
        if entry_id:
            by_entry[entry_id] = row
        if registration_id:
            by_registration.setdefault(registration_id, []).append(row)
    return by_entry, by_registration


def _legacy_for_entry(
    entry: Mapping[str, Any],
    by_entry: Mapping[str, Mapping[str, Any]],
    by_registration: Mapping[str, list[Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    entry_id = _row_get(entry, "ENTRY_ID")
    if entry_id and entry_id in by_entry:
        return by_entry[entry_id]

    registration_id = _row_get(entry, "REGISTRATION_ID")
    candidates = list(by_registration.get(registration_id, []))
    if not candidates:
        return None

    event_code = _row_get(entry, "EVENT_CODE")
    event_name = _row_get(entry, "EVENT_NAME")
    for row in candidates:
        if event_code and _row_get(row, "EVENT_CODE", "event_code") == event_code:
            return row
        if event_name and _row_get(row, "EVENT", "event", "EVENT_NAME", "event_name") == event_name:
            return row
    return candidates[0]


def _fallback_name_parts(full_name: Any) -> tuple[str, str, str]:
    """Conservative fallback used only when structured/legacy fields are absent."""
    tokens = [part for part in re.split(r"\s+", _clean(full_name)) if part]
    if not tokens:
        return "", "", ""
    if len(tokens) == 1:
        return tokens[0], "", ""
    return " ".join(tokens[:-1]), "", tokens[-1]


def _name_parts(
    entry: Mapping[str, Any],
    registration: Mapping[str, Any] | None,
    legacy_row: Mapping[str, Any] | None,
) -> tuple[str, str, str, str]:
    """Return (first, other, last, source)."""
    for source, row in (
        ("EVENT_ENTRIES", entry),
        ("REGISTRATIONS", registration),
        ("OUTPUT", legacy_row),
    ):
        if not row:
            continue
        first = _row_get(row, "FIRST_NAME", "first_name")
        other = _row_get(row, "OTHER_NAME", "other_name")
        last = _row_get(row, "LAST_NAME", "last_name")
        if first or other or last:
            return first, other, last, source

    full_name = (
        _row_get(entry, "ATHLETE_NAME", "athlete_name", "FULL_NAME", "full_name", "NAME", "name")
        or _row_get(registration, "ATHLETE_NAME", "athlete_name", "FULL_NAME", "full_name", "NAME", "name")
        or _row_get(legacy_row, "FULL_NAME", "full_name", "NAME", "name")
    )
    first, other, last = _fallback_name_parts(full_name)
    return first, other, last, "FALLBACK"


def _format_line(fields: list[Any]) -> str:
    return "; ".join(_safe_field(value) for value in fields)


def _build_i_fields(row: Mapping[str, Any]) -> list[str]:
    return [
        "I",
        _row_get(row, "LAST_NAME"),
        _row_get(row, "FIRST_NAME"),
        "",
        _gender_code(_row_get(row, "GENDER")),
        _format_date(_row_get(row, "DOB")),
        _row_get(row, "TEAM_CODE"),
        _row_get(row, "TEAM_NAME"),
        "", "", "", "", "", "", "", "",
        _row_get(row, "NATIONALITY"),
        "", "", "", "",
        _row_get(row, "ATHLETE_ID"),
        "", "", "",
    ]


def _build_e_fields(row: Mapping[str, Any]) -> list[str]:
    return [
        "E",
        _row_get(row, "LAST_NAME"),
        _row_get(row, "FIRST_NAME"),
        "",
        _gender_code(_row_get(row, "GENDER")),
        _format_date(_row_get(row, "DOB")),
        _row_get(row, "TEAM_CODE"),
        _row_get(row, "TEAM_NAME"),
        "",
        "",
        export_event_code(_row_get(row, "EVENT_NAME"), _row_get(row, "EVENT_CODE")),
        _row_get(row, "SEASON_BEST"),
        "",
        "M",
        _format_division(_row_get(row, "DIVISION")),
        "",
        "",
    ]


def build_hytek_text(rows: Iterable[Mapping[str, Any]], record_type: str) -> str:
    record_type = _upper(record_type)[:1] or "I"
    if record_type not in {"I", "E"}:
        raise ValueError("record_type must be 'I' or 'E'.")

    builder = _build_i_fields if record_type == "I" else _build_e_fields
    expected = I_FIELD_COUNT if record_type == "I" else E_FIELD_COUNT
    lines: list[str] = []
    for row in rows:
        fields = builder(row)
        if len(fields) != expected:  # pragma: no cover - programming invariant
            raise RuntimeError(
                f"{record_type} row has {len(fields)} fields; expected {expected}."
            )
        lines.append(_format_line(fields))
    return "\n".join(lines) + ("\n" if lines else "")


def _active_confirmed(row: Mapping[str, Any]) -> bool:
    return _upper(row.get("STATUS")) == "CONFIRMED" and not _truthy(row.get("IS_DELETED"))


def build_transactional_hytek_exports(
    event_entries: Iterable[Mapping[str, Any]] | pd.DataFrame,
    registrations: Iterable[Mapping[str, Any]] | pd.DataFrame,
    *,
    competition_id: str = "",
    legacy_output_rows: Iterable[Mapping[str, Any]] | pd.DataFrame | None = None,
) -> HyTekExportBundle:
    """Build I and E exports from authoritative transaction rows.

    Export eligibility is intentionally strict:
      * EVENT_ENTRIES.STATUS must be CONFIRMED
      * EVENT_ENTRIES.IS_DELETED must not be true
      * the parent REGISTRATIONS row must exist, be CONFIRMED and not deleted

    Legacy OUTPUT data is never used to decide whether an entry is exported. It
    is only a transitional source of structured name fields for historical rows.
    """
    entries = _mapping_rows(event_entries)
    registration_rows = _mapping_rows(registrations)
    registration_by_id = {
        _row_get(row, "REGISTRATION_ID"): row
        for row in registration_rows
        if _row_get(row, "REGISTRATION_ID")
    }
    legacy_by_entry, legacy_by_registration = _legacy_indexes(legacy_output_rows)

    competition_filter = _clean(competition_id)
    export_rows: list[dict[str, Any]] = []
    exported_ids: list[str] = []
    excluded_ids: list[str] = []
    diagnostics: list[HyTekDiagnostic] = []

    for entry in entries:
        entry_id = _row_get(entry, "ENTRY_ID")
        entry_competition = _row_get(entry, "COMPETITION_ID")
        if competition_filter and entry_competition != competition_filter:
            continue

        if not _active_confirmed(entry):
            excluded_ids.append(entry_id)
            diagnostics.append(
                HyTekDiagnostic(
                    "INFO",
                    "ENTRY_NOT_EXPORTABLE",
                    entry_id,
                    "Entry is not CONFIRMED or is deleted/withdrawn.",
                )
            )
            continue

        registration_id = _row_get(entry, "REGISTRATION_ID")
        registration = registration_by_id.get(registration_id)
        if registration is None:
            excluded_ids.append(entry_id)
            diagnostics.append(
                HyTekDiagnostic(
                    "ERROR",
                    "ORPHAN_REGISTRATION",
                    entry_id,
                    f"No parent REGISTRATIONS row exists for {registration_id or 'blank REGISTRATION_ID'}.",
                )
            )
            continue

        if not _active_confirmed(registration):
            excluded_ids.append(entry_id)
            diagnostics.append(
                HyTekDiagnostic(
                    "ERROR",
                    "PARENT_NOT_EXPORTABLE",
                    entry_id,
                    "Parent registration is not CONFIRMED or is deleted.",
                )
            )
            continue

        legacy_row = _legacy_for_entry(entry, legacy_by_entry, legacy_by_registration)
        first_name, other_name, last_name, name_source = _name_parts(
            entry, registration, legacy_row
        )
        if name_source == "FALLBACK":
            diagnostics.append(
                HyTekDiagnostic(
                    "WARNING",
                    "NAME_FALLBACK",
                    entry_id,
                    "Structured FIRST_NAME/LAST_NAME were unavailable; ATHLETE_NAME was split as a fallback.",
                )
            )

        merged = {
            "ENTRY_ID": entry_id,
            "REGISTRATION_ID": registration_id,
            "COMPETITION_ID": entry_competition or _row_get(registration, "COMPETITION_ID"),
            "FIRST_NAME": first_name,
            "OTHER_NAME": other_name,
            "LAST_NAME": last_name,
            "GENDER": _row_get(entry, "GENDER") or _row_get(registration, "GENDER"),
            "DOB": _row_get(entry, "DOB") or _row_get(registration, "DOB"),
            "TEAM_CODE": _row_get(entry, "TEAM_CODE") or _row_get(registration, "TEAM_CODE"),
            "TEAM_NAME": _row_get(entry, "TEAM_NAME") or _row_get(registration, "TEAM_NAME"),
            "NATIONALITY": _row_get(entry, "NATIONALITY") or _row_get(registration, "NATIONALITY"),
            "ATHLETE_ID": _row_get(entry, "ATHLETE_ID") or _row_get(registration, "ATHLETE_ID"),
            "EVENT_NAME": _row_get(entry, "EVENT_NAME"),
            "EVENT_CODE": _row_get(entry, "EVENT_CODE"),
            "SEASON_BEST": _row_get(entry, "SEASON_BEST"),
            "DIVISION": _row_get(entry, "DIVISION") or _row_get(registration, "DIVISION"),
        }
        export_rows.append(merged)
        exported_ids.append(entry_id)

    return HyTekExportBundle(
        competition_id=competition_filter,
        i_text=build_hytek_text(export_rows, "I"),
        e_text=build_hytek_text(export_rows, "E"),
        exported_entry_ids=tuple(exported_ids),
        excluded_entry_ids=tuple(excluded_ids),
        diagnostics=tuple(diagnostics),
    )


def build_legacy_output_hytek_export(
    sheet_df: pd.DataFrame,
    record_type: str = "I",
) -> str:
    """Compatibility renderer for the legacy OUTPUT-sheet download feature."""
    if sheet_df is None or sheet_df.empty:
        return ""

    rows: list[dict[str, Any]] = []
    for source in _mapping_rows(sheet_df):
        # Historical rows may pre-date transactional status columns. If status
        # data exists, respect soft deletion/withdrawal; otherwise preserve the
        # old private-site behavior and include the row.
        if _truthy(_row_get(source, "IS_DELETED", "is_deleted")):
            continue
        entry_status = _upper(_row_get(source, "ENTRY_STATUS", "entry_status"))
        if entry_status == "WITHDRAWN":
            continue

        row = {
            "FIRST_NAME": _row_get(source, "FIRST_NAME", "first_name"),
            "LAST_NAME": _row_get(source, "LAST_NAME", "last_name"),
            "GENDER": _row_get(source, "GENDER", "gender"),
            "DOB": _row_get(source, "DOB", "dob", "BIRTH_DATE", "birth_date", "DATE_OF_BIRTH", "date_of_birth"),
            "TEAM_CODE": _row_get(source, "TEAM_CODE", "team_code"),
            "TEAM_NAME": _row_get(source, "TEAM_NAME", "team_name"),
            "NATIONALITY": _row_get(source, "NATIONALITY", "nationality"),
            "ATHLETE_ID": _row_get(source, "UNIQUE_ID", "unique_id", "ATHLETE_ID", "athlete_id"),
            "EVENT_NAME": _row_get(source, "EVENT", "event", "EVENT_NAME", "event_name"),
            "EVENT_CODE": _row_get(source, "EVENT_CODE", "event_code"),
            "SEASON_BEST": _row_get(source, "SEASON_BEST", "season_best"),
            "DIVISION": _row_get(source, "EVENT_DIVISION", "event_division", "DIVISION", "division"),
        }
        if any(_clean(v) for v in row.values()):
            rows.append(row)

    return build_hytek_text(rows, record_type)
