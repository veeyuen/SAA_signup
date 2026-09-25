from __future__ import annotations

import re


def _open_spreadsheet(gc, sheet_url_or_id: str):
    ref = str(sheet_url_or_id or "").strip()
    if ref.startswith("http"):
        return gc.open_by_url(ref)
    return gc.open_by_key(ref)


def _normalize_header(value: str) -> str:
    value = str(value or "").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _ensure_column_capacity(worksheet, required_columns: int) -> None:
    """Expand the Google Sheet grid before writing headers past its last column."""
    current_columns = int(getattr(worksheet, "col_count", 0) or 0)
    if current_columns >= required_columns:
        return

    columns_to_add = required_columns - current_columns
    try:
        worksheet.add_cols(columns_to_add)
    except AttributeError:
        # Compatibility fallback for older gspread versions.
        worksheet.resize(cols=required_columns)


def _entry_value_for_header(entry: dict, normalized_header: str):
    aliases = {
        "dob": "birth_date",
        "date_of_birth": "birth_date",
        "birthdate": "birth_date",
        "ic_number_last_4": "ic_last4",
        "ic_last_4": "ic_last4",
        "nric_last_4": "ic_last4",
        "name_as_per_nric_passport": "name_passport",
        "name_as_per_nric_or_passport": "name_passport",
        "contact": "contact_number",
        "division": "event_division",
        "event_name": "event",
        "sg_pr": "singapore_pr",
    }
    key = aliases.get(normalized_header, normalized_header)
    value = entry.get(key, "")

    if key == "singapore_pr" and isinstance(value, bool):
        return "Yes" if value else "No"

    return value


def append_confirmed_entries_if_missing(
    *,
    gc,
    output_sheet_url_or_id: str,
    output_worksheet: str,
    order_id: str,
    entry_rows: list[dict],
    stripe_session_id: str,
    stripe_payment_intent_id: str,
) -> bool:
    """Append confirmed rows once, using ORDER_ID for idempotency.

    Each event row keeps its original athlete-level REGISTRATION_ID. This is
    required for multi-athlete orders where one ORDER_ID contains more than
    one registration.
    """
    spreadsheet = _open_spreadsheet(gc, output_sheet_url_or_id)
    worksheet = (
        spreadsheet.worksheet(output_worksheet)
        if str(output_worksheet or "").strip()
        else spreadsheet.sheet1
    )

    headers = worksheet.row_values(1)

    required_extra = [
        "entry_id",
        "order_id",
        "payment_id",
        "registration_id",
        "payment_status",
        "entry_status",
        "is_deleted",
        "stripe_checkout_session_id",
        "stripe_payment_intent_id",
    ]

    if not headers:
        base_headers = list(entry_rows[0].keys()) if entry_rows else []
        normalized_base = [_normalize_header(h) for h in base_headers]
        headers = base_headers + [
            h
            for h in required_extra
            if _normalize_header(h) not in normalized_base
        ]

        _ensure_column_capacity(worksheet, len(headers))
        worksheet.append_row(headers)
    else:
        normalized = [_normalize_header(h) for h in headers]
        missing_headers = [
            extra
            for extra in required_extra
            if extra not in normalized
        ]

        # Google Sheets worksheets have a fixed grid size. The legacy OUTPUT
        # sheet currently has 27 columns, so writing AB1 (column 28) fails
        # unless the grid is expanded first.
        _ensure_column_capacity(
            worksheet,
            len(headers) + len(missing_headers),
        )

        for extra in missing_headers:
            headers.append(extra)
            worksheet.update_cell(1, len(headers), extra)
            normalized.append(extra)

    normalized_headers = [_normalize_header(h) for h in headers]
    order_col = normalized_headers.index("order_id") + 1

    existing_order_ids = {
        str(value).strip()
        for value in worksheet.col_values(order_col)[1:]
        if str(value).strip()
    }
    if order_id in existing_order_ids:
        return False

    existing_rows = worksheet.get_all_values()
    next_no = max(len(existing_rows), 1)

    rows_to_append = []
    for offset, entry in enumerate(entry_rows):
        enriched = dict(entry)

        # Preserve the event row's original registration_id.
        enriched["order_id"] = order_id
        enriched["payment_status"] = "PAYMENT_COMPLETE"
        enriched["entry_status"] = "CONFIRMED"
        enriched["is_deleted"] = False
        enriched["stripe_checkout_session_id"] = stripe_session_id
        enriched["stripe_payment_intent_id"] = stripe_payment_intent_id

        row_values = []
        for header in normalized_headers:
            if header in ("no", "number"):
                row_values.append(next_no + offset)
            else:
                row_values.append(
                    _entry_value_for_header(enriched, header)
                )
        rows_to_append.append(row_values)

    if rows_to_append:
        worksheet.append_rows(
            rows_to_append,
            value_input_option="USER_ENTERED",
        )

    return True


def update_entry_payment_status(
    *,
    gc,
    output_sheet_url_or_id: str,
    output_worksheet: str,
    entry_id: str,
    payment_status: str,
) -> int:
    """Update compatibility OUTPUT payment_status for one transaction entry.

    Phase 3B refund webhooks use ENTRY_ID as the stable join key. If the OUTPUT
    row is absent, return 0 rather than failing the authoritative transaction
    update.
    """
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        return 0

    spreadsheet = _open_spreadsheet(gc, output_sheet_url_or_id)
    worksheet = (
        spreadsheet.worksheet(output_worksheet)
        if str(output_worksheet or "").strip()
        else spreadsheet.sheet1
    )

    headers = worksheet.row_values(1)
    normalized = [_normalize_header(h) for h in headers]
    missing = [h for h in ["entry_id", "payment_status"] if h not in normalized]
    if missing:
        _ensure_column_capacity(worksheet, len(headers) + len(missing))
        for header in missing:
            headers.append(header)
            worksheet.update_cell(1, len(headers), header)
            normalized.append(header)

    entry_col = normalized.index("entry_id") + 1
    payment_col = normalized.index("payment_status") + 1
    try:
        entry_values = worksheet.col_values(entry_col)
    except Exception:
        return 0

    count = 0
    for row_number, value in enumerate(entry_values, start=1):
        if row_number == 1:
            continue
        if str(value or "").strip() == entry_id:
            worksheet.update_cell(row_number, payment_col, str(payment_status or "").strip())
            count += 1
    return count


def update_entry_fee_and_payment_status(
    *,
    gc,
    output_sheet_url_or_id: str,
    output_worksheet: str,
    entry_id: str,
    entry_fee: str,
    payment_status: str = "PAYMENT_COMPLETE",
) -> int:
    """Update one compatibility OUTPUT row after a paid fee increase.

    The authoritative transaction write happens first. OUTPUT is only a
    compatibility projection and is joined by stable ENTRY_ID.
    """
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        return 0

    spreadsheet = _open_spreadsheet(gc, output_sheet_url_or_id)
    worksheet = (
        spreadsheet.worksheet(output_worksheet)
        if str(output_worksheet or "").strip()
        else spreadsheet.sheet1
    )

    headers = worksheet.row_values(1)
    normalized = [_normalize_header(h) for h in headers]
    required = ["entry_id", "entry_fee", "payment_status"]
    missing = [h for h in required if h not in normalized]
    if missing:
        _ensure_column_capacity(worksheet, len(headers) + len(missing))
        for header in missing:
            headers.append(header)
            worksheet.update_cell(1, len(headers), header)
            normalized.append(header)

    entry_col = normalized.index("entry_id") + 1
    fee_col = normalized.index("entry_fee") + 1
    payment_col = normalized.index("payment_status") + 1
    try:
        entry_values = worksheet.col_values(entry_col)
    except Exception:
        return 0

    count = 0
    for row_number, value in enumerate(entry_values, start=1):
        if row_number == 1:
            continue
        if str(value or "").strip() != entry_id:
            continue
        worksheet.update_cell(row_number, fee_col, str(entry_fee or "").strip())
        worksheet.update_cell(
            row_number,
            payment_col,
            str(payment_status or "PAYMENT_COMPLETE").strip(),
        )
        count += 1
    return count
