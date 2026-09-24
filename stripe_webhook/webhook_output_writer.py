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
        "order_id",
        "payment_id",
        "registration_id",
        "payment_status",
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
        worksheet.append_row(headers)
    else:
        normalized = [_normalize_header(h) for h in headers]
        for extra in required_extra:
            if extra not in normalized:
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
