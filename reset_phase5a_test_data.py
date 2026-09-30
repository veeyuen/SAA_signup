#!/usr/bin/env python3
"""
Reset SAA signup transactional test data for a fresh Phase 5A test cycle.

Safety:
- DRY RUN by default.
- Preserves worksheet tabs and row-1 headers.
- Backs up every targeted worksheet to CSV before clearing.
- Does NOT touch configuration/master tabs.
- Requires --apply to actually clear data.

Authentication:
1) Preferred: --service-account-json /path/to/service-account.json
2) Otherwise: Application Default Credentials (gcloud auth application-default login)

Example:
python reset_phase5a_test_data.py \
  --sheet-url "https://docs.google.com/spreadsheets/d/..." \
  --service-account-json "/path/to/service-account.json"

Then, after reviewing the dry run:
python reset_phase5a_test_data.py \
  --sheet-url "https://docs.google.com/spreadsheets/d/..." \
  --service-account-json "/path/to/service-account.json" \
  --apply
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path
import sys

import gspread
from gspread.utils import rowcol_to_a1

TRANSACTION_TABS = [
    "ORDERS",
    "REGISTRATIONS",
    "EVENT_ENTRIES",
    "PAYMENTS",
    "WAIVERS",
    "REFUNDS",
    "MOE_INVOICES",
    "AUDIT_LOG",
    "NOTIFICATIONS",
]

LEGACY_TABS = [
    "OUTPUT",
    "PendingPayments",
]

PRESERVE_TABS = [
    "SYSTEM_CONFIG",
    "LOOKUPS",
    "DIVISIONS",
    "EVENTS_MASTER",
    "ORGANIZATIONS",
    "USERS",
    "COMPETITIONS",
    "COMPETITION_FEES",
    "EVENT_CONFIG",
]


def get_client(service_account_json: str | None):
    if service_account_json:
        return gspread.service_account(filename=service_account_json)

    import google.auth

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials, _ = google.auth.default(scopes=scopes)
    return gspread.authorize(credentials)


def backup_worksheet(ws, backup_dir: Path) -> int:
    values = ws.get_all_values()
    backup_path = backup_dir / f"{ws.title}.csv"
    with backup_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(values)
    return max(0, len(values) - 1)


def clear_data_rows(ws):
    # Preserve row 1 (headers) and clear only rows 2 onward.
    if ws.row_count < 2:
        return

    last_cell = rowcol_to_a1(ws.row_count, ws.col_count)
    # rowcol_to_a1 returns e.g. AD1000. Use the last column with row_count.
    import re
    match = re.match(r"([A-Z]+)\d+$", last_cell)
    if not match:
        raise RuntimeError(f"Could not determine last column for {ws.title}")
    last_col = match.group(1)
    ws.batch_clear([f"A2:{last_col}{ws.row_count}"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-url", required=True, help="Operational Google Sheet URL")
    parser.add_argument(
        "--service-account-json",
        default="",
        help="Path to Google service-account JSON. If omitted, ADC is used.",
    )
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also clear OUTPUT and PendingPayments if they exist.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear the data. Without this flag, only a dry run is performed.",
    )
    args = parser.parse_args()

    gc = get_client(args.service_account_json or None)
    book = gc.open_by_url(args.sheet_url)

    available = {ws.title: ws for ws in book.worksheets()}
    targets = list(TRANSACTION_TABS)
    if args.include_legacy:
        targets.extend(LEGACY_TABS)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path.cwd() / f"phase5a_reset_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"Workbook: {book.title}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Backup directory: {backup_dir}")
    print()
    print("Configuration/master tabs that will NOT be touched:")
    for name in PRESERVE_TABS:
        print(f"  KEEP  {name}")
    print()

    missing = []
    planned = []

    for name in targets:
        ws = available.get(name)
        if ws is None:
            missing.append(name)
            continue

        row_count = backup_worksheet(ws, backup_dir)
        planned.append((name, row_count))
        print(f"  {'CLEAR' if args.apply else 'WOULD CLEAR'}  {name}: {row_count} data row(s)")

    if missing:
        print()
        print("Tabs not found (skipped):")
        for name in missing:
            print(f"  SKIP  {name}")

    if not args.apply:
        print()
        print("DRY RUN ONLY: no sheet data was changed.")
        print("Review the list and backup CSVs, then rerun with --apply.")
        return 0

    print()
    confirmation = input(
        "Type RESET PHASE 5A exactly to clear the listed data rows: "
    ).strip()

    if confirmation != "RESET PHASE 5A":
        print("Confirmation did not match. Nothing was cleared.")
        return 2

    for name, _ in planned:
        clear_data_rows(available[name])
        print(f"  CLEARED  {name}")

    print()
    print("Phase 5A reset complete.")
    print(f"Backup retained at: {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
