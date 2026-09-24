from __future__ import annotations

import re
from typing import Any


class OutputAdminError(RuntimeError):
    pass


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalise_header(value: Any) -> str:
    value = _clean(value).casefold()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _open_spreadsheet(gc, sheet_url_or_id: str):
    ref = _clean(sheet_url_or_id)
    if not ref:
        raise OutputAdminError("OUTPUT_SHEET_URL is empty.")
    if ref.startswith("http"):
        return gc.open_by_url(ref)
    return gc.open_by_key(ref)


def _ensure_headers(worksheet, required: list[str]) -> tuple[list[str], dict[str, int]]:
    headers = worksheet.row_values(1)
    normalised = [_normalise_header(h) for h in headers]
    missing = [h for h in required if _normalise_header(h) not in normalised]

    required_cols = len(headers) + len(missing)
    current_cols = int(getattr(worksheet, "col_count", 0) or 0)
    if current_cols < required_cols:
        try:
            worksheet.add_cols(required_cols - current_cols)
        except AttributeError:
            worksheet.resize(cols=required_cols)

    for header in missing:
        headers.append(header)
        worksheet.update_cell(1, len(headers), header)
        normalised.append(_normalise_header(header))

    return headers, {
        _normalise_header(header): idx + 1
        for idx, header in enumerate(headers)
    }


def _matching_rows(worksheet, header_map: dict[str, int], entry: dict[str, Any]) -> list[int]:
    """Match OUTPUT rows by ENTRY_ID, falling back to registration + event.

    Older pilot OUTPUT rows predate ENTRY_ID, so the fallback preserves the
    ability to amend/withdraw those rows without physically deleting them.
    """
    entry_id = _clean(entry.get("ENTRY_ID"))
    entry_col = header_map.get("entry_id")
    if entry_id and entry_col:
        values = worksheet.col_values(entry_col)
        matches = [
            i for i, value in enumerate(values, start=1)
            if i > 1 and _clean(value) == entry_id
        ]
        if matches:
            return matches

    registration_id = _clean(entry.get("REGISTRATION_ID"))
    event_name = _clean(entry.get("EVENT_NAME"))
    registration_col = header_map.get("registration_id")
    event_col = header_map.get("event") or header_map.get("event_name")
    if not registration_id or not event_name or not registration_col or not event_col:
        return []

    registration_values = worksheet.col_values(registration_col)
    event_values = worksheet.col_values(event_col)
    max_len = max(len(registration_values), len(event_values))
    out = []
    for i in range(2, max_len + 1):
        reg = registration_values[i - 1] if i - 1 < len(registration_values) else ""
        evt = event_values[i - 1] if i - 1 < len(event_values) else ""
        if _clean(reg) == registration_id and _clean(evt).casefold() == event_name.casefold():
            out.append(i)
    return out


def sync_output_entry(
    *,
    gc,
    output_sheet_url_or_id: str,
    output_worksheet: str,
    entry: dict[str, Any],
    season_best: str | None = None,
    status: str | None = None,
    is_deleted: bool | None = None,
) -> int:
    """Update the compatibility OUTPUT projection for one event entry."""
    spreadsheet = _open_spreadsheet(gc, output_sheet_url_or_id)
    worksheet = (
        spreadsheet.worksheet(output_worksheet)
        if _clean(output_worksheet)
        else spreadsheet.sheet1
    )

    _, header_map = _ensure_headers(
        worksheet,
        ["entry_id", "entry_status", "is_deleted"],
    )
    rows = _matching_rows(worksheet, header_map, entry)
    for row_number in rows:
        if _clean(entry.get("ENTRY_ID")):
            worksheet.update_cell(row_number, header_map["entry_id"], _clean(entry.get("ENTRY_ID")))
        if season_best is not None:
            season_col = header_map.get("season_best")
            if season_col:
                worksheet.update_cell(row_number, season_col, _clean(season_best))
        if status is not None:
            worksheet.update_cell(row_number, header_map["entry_status"], _clean(status))
        if is_deleted is not None:
            worksheet.update_cell(row_number, header_map["is_deleted"], "TRUE" if is_deleted else "FALSE")
    return len(rows)
