# payment_store.py

from __future__ import annotations

import datetime as dt
import json

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


PENDING_HEADERS = [
    "registration_id",
    "created_at",
    "status",
    "login_email",
    "athlete_email",
    "full_name",
    "team_name",
    "events_json",
    "entry_rows_json",
    "amount",
    "currency",
    "stripe_checkout_session_id",
    "stripe_payment_intent_id",
    "confirmed_at",
    "ack_email_sent",
    "error",
]


def create_google_client(service_account_info: dict):
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )
    return gspread.authorize(credentials)


def get_pending_worksheet(
    gc,
    sheet_url: str,
    worksheet_name: str,
):
    spreadsheet = gc.open_by_url(sheet_url)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=1000,
            cols=len(PENDING_HEADERS),
        )
        worksheet.append_row(PENDING_HEADERS)

    if not worksheet.row_values(1):
        worksheet.append_row(PENDING_HEADERS)

    return worksheet


def save_pending_registration(
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
):
    worksheet.append_row(
        [
            registration_id,
            dt.datetime.now(dt.timezone.utc).isoformat(),
            "PENDING",
            login_email,
            athlete_email,
            full_name,
            team_name,
            json.dumps(events, ensure_ascii=False),
            json.dumps(entry_rows, ensure_ascii=False, default=str),
            amount,
            currency,
            stripe_session_id,
            "",
            "",
            "No",
            "",
        ],
        value_input_option="USER_ENTERED",
    )


def find_pending_registration(
    worksheet,
    registration_id: str,
):
    records = worksheet.get_all_records()

    for row_number, record in enumerate(records, start=2):
        if str(record.get("registration_id", "")).strip() == registration_id:
            return row_number, record

    return None, None


def update_pending_fields(
    worksheet,
    row_number: int,
    **values,
):
    headers = worksheet.row_values(1)
    column_map = {
        header: index + 1
        for index, header in enumerate(headers)
    }

    updates = []

    for key, value in values.items():
        column_number = column_map.get(key)
        if not column_number:
            continue

        updates.append(
            {
                "range": gspread.utils.rowcol_to_a1(
                    row_number,
                    column_number,
                ),
                "values": [[value]],
            }
        )

    if updates:
        worksheet.batch_update(updates)
