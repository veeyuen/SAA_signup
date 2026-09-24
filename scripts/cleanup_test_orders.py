#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import gspread
from google.oauth2 import service_account


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def open_spreadsheet(gc, ref: str):
    return gc.open_by_url(ref) if str(ref).startswith("http") else gc.open_by_key(ref)


def matching_rows(worksheet, key_header: str, order_ids: set[str]):
    values = worksheet.get_all_values()
    if not values:
        return []
    header_map = {norm(h): i for i, h in enumerate(values[0])}
    idx = header_map.get(norm(key_header))
    if idx is None:
        return []
    rows = []
    for row_number, row in enumerate(values[1:], start=2):
        value = row[idx] if idx < len(row) else ""
        if str(value or "").strip() in order_ids:
            rows.append(row_number)
    return rows


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run by default. Remove explicitly named SAA test orders from "
            "transaction, PendingPayments and OUTPUT tabs only when --apply is supplied."
        )
    )
    parser.add_argument("--sheet-url", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--order-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    order_ids = {str(x).strip() for x in args.order_id if str(x).strip()}
    if not order_ids:
        raise SystemExit("No order IDs supplied.")

    credentials = service_account.Credentials.from_service_account_file(
        str(Path(args.service_account).expanduser()),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(credentials)
    spreadsheet = open_spreadsheet(gc, args.sheet_url)

    targets = [
        ("ORDERS", "ORDER_ID"),
        ("REGISTRATIONS", "ORDER_ID"),
        ("EVENT_ENTRIES", "ORDER_ID"),
        ("PAYMENTS", "ORDER_ID"),
        ("WAIVERS", "ORDER_ID"),
        ("PendingPayments", "registration_id"),
        ("OUTPUT", "order_id"),
    ]

    total = 0
    plan = []
    for sheet_name, key_header in targets:
        try:
            ws = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            plan.append((sheet_name, []))
            continue
        rows = matching_rows(ws, key_header, order_ids)
        total += len(rows)
        plan.append((sheet_name, rows))

    print("Order IDs:", ", ".join(sorted(order_ids)))
    print("Mode:", "APPLY" if args.apply else "DRY RUN")
    for sheet_name, rows in plan:
        print(f"{sheet_name}: {len(rows)} matching row(s)" + (f" -> {rows}" if rows else ""))
    print("Total matching rows:", total)

    if not args.apply:
        print("No rows deleted. Re-run with --apply only after reviewing the dry-run counts.")
        return

    for sheet_name, rows in plan:
        if not rows:
            continue
        ws = spreadsheet.worksheet(sheet_name)
        # Delete from bottom to top so row numbers remain valid.
        for row_number in sorted(rows, reverse=True):
            ws.delete_rows(row_number)

    print("Cleanup complete.")


if __name__ == "__main__":
    main()
