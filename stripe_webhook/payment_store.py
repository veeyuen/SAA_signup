from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def create_google_client(service_account_info: dict):
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )
    return gspread.authorize(credentials)


def _open_spreadsheet(gc, sheet_url_or_id: str):
    ref = str(sheet_url_or_id or "").strip()
    if ref.startswith("http"):
        return gc.open_by_url(ref)
    return gc.open_by_key(ref)


def get_pending_worksheet(gc, sheet_url_or_id: str, worksheet_name: str):
    spreadsheet = _open_spreadsheet(gc, sheet_url_or_id)
    return spreadsheet.worksheet(worksheet_name)


def find_pending_registration(worksheet, registration_id: str):
    for row_number, record in enumerate(
        worksheet.get_all_records(),
        start=2,
    ):
        if (
            str(record.get("registration_id", "")).strip()
            == str(registration_id or "").strip()
        ):
            return row_number, record
    return None, None


def update_pending_fields(worksheet, row_number: int, **values):
    headers = worksheet.row_values(1)
    column_map = {
        str(header).strip(): index + 1
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
