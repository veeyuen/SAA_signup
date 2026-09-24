from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _update_row(worksheet, row_number: int, row_values: list[Any]) -> None:
    end_col = _column_letter(len(row_values))
    range_name = f"A{row_number}:{end_col}{row_number}"
    try:
        worksheet.update(range_name=range_name, values=[row_values])
    except TypeError:
        # gspread < 6 compatibility
        worksheet.update(range_name, [row_values])


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def upsert_pending_registration(
    *,
    worksheet,
    registration_id: str,
    login_email: str,
    athlete_email: str,
    full_name: str,
    team_name: str,
    events: list[str],
    entry_rows: list[dict],
    amount: str,
    currency: str,
    stripe_session_id: str,
) -> int:
    """Maintain one current PendingPayments row per order.

    If a previous row exists for the order, it is updated in place rather than
    appending another row. This prevents duplicate pending rows when Checkout is
    retried or the Streamlit action is triggered twice.
    """
    values = worksheet.get_all_values()
    if not values:
        raise RuntimeError("PendingPayments sheet has no header row.")

    headers = values[0]
    header_map = {_norm(h): i for i, h in enumerate(headers)}
    reg_idx = header_map.get("registration_id")
    if reg_idx is None:
        raise RuntimeError("PendingPayments is missing registration_id header.")

    registration_id = str(registration_id or "").strip()
    stripe_session_id = str(stripe_session_id or "").strip()

    matches: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(values[1:], start=2):
        cell = row[reg_idx] if reg_idx < len(row) else ""
        if str(cell or "").strip() == registration_id:
            padded = list(row) + [""] * max(0, len(headers) - len(row))
            matches.append((row_number, padded[: len(headers)]))

    # Prefer a row already tied to this exact Stripe session. Otherwise update
    # the newest row for the order, consolidating future retries into one row.
    target: tuple[int, list[str]] | None = None
    session_idx = header_map.get("stripe_checkout_session_id")
    if session_idx is not None and stripe_session_id:
        for candidate in matches:
            if str(candidate[1][session_idx] or "").strip() == stripe_session_id:
                target = candidate
                break
    if target is None and matches:
        target = matches[-1]

    field_values = {
        "registration_id": registration_id,
        "created_at": _now_iso(),
        "status": "PENDING",
        "login_email": str(login_email or "").strip(),
        "athlete_email": str(athlete_email or "").strip(),
        "full_name": str(full_name or "").strip(),
        "team_name": str(team_name or "").strip(),
        "events_json": json.dumps(events or [], ensure_ascii=False),
        "entry_rows_json": json.dumps(entry_rows or [], ensure_ascii=False),
        "amount": str(amount or "").strip(),
        "currency": str(currency or "").strip().lower(),
        "stripe_checkout_session_id": stripe_session_id,
        "stripe_payment_intent_id": "",
        "confirmed_at": "",
        "ack_email_sent": "No",
        "error": "",
    }

    if target is None:
        row_values = [""] * len(headers)
        for name, value in field_values.items():
            idx = header_map.get(name)
            if idx is not None:
                row_values[idx] = value
        worksheet.append_row(row_values, value_input_option="USER_ENTERED")
        return len(values) + 1

    row_number, row_values = target
    current_status = ""
    status_idx = header_map.get("status")
    if status_idx is not None:
        current_status = str(row_values[status_idx] or "").strip().upper()

    # Never downgrade an already-paid order back to PENDING.
    if current_status == "PAID":
        return row_number

    # Preserve the original created_at on retries.
    field_values.pop("created_at", None)
    for name, value in field_values.items():
        idx = header_map.get(name)
        if idx is not None:
            row_values[idx] = value

    _update_row(worksheet, row_number, row_values)
    return row_number
