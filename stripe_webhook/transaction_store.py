from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
from typing import Any


class TransactionStoreError(RuntimeError):
    pass


SHEETS: dict[str, list[str]] = {
    "ORDERS": [
        "ORDER_ID",
        "COMPETITION_ID",
        "USER_ID",
        "ORGANIZATION_ID",
        "ENTRY_COUNT",
        "ENTRY_SUBTOTAL",
        "PROCESSING_FEE",
        "TOTAL_AMOUNT",
        "PAYMENT_TYPE",
        "STATUS",
        "CREATED_AT",
        "EXPIRES_AT",
        "UPDATED_AT",
    ],
    "REGISTRATIONS": [
        "REGISTRATION_ID",
        "ORDER_ID",
        "COMPETITION_ID",
        "ATHLETE_ID",
        "ORGANIZATION_ID",
        "SUBMITTED_BY_USER_ID",
        "DIVISION",
        "STATUS",
        "CREATED_AT",
        "UPDATED_AT",
        "IS_DELETED",
        "ATHLETE_NAME",
        "DOB",
        "GENDER",
        "NATIONALITY",
        "TEAM_CODE",
        "TEAM_NAME",
        "EMAIL",
        "CONTACT_NUMBER",
    ],
    "EVENT_ENTRIES": [
        "ENTRY_ID",
        "REGISTRATION_ID",
        "ORDER_ID",
        "PAYMENT_ID",
        "COMPETITION_ID",
        "ATHLETE_ID",
        "ORGANIZATION_ID",
        "EVENT_NAME",
        "EVENT_CODE",
        "DIVISION",
        "SEASON_BEST",
        "ENTRY_FEE",
        "REGISTRATION_PERIOD",
        "PAYMENT_STATUS",
        "PAYMENT_STATUS_CHANGED_AT",
        "STATUS",
        "CREATED_AT",
        "UPDATED_AT",
        "IS_DELETED",
        "ATHLETE_NAME",
        "DOB",
        "GENDER",
        "NATIONALITY",
        "TEAM_CODE",
        "TEAM_NAME",
        "EMAIL",
        "CONTACT_NUMBER",
    ],
    "PAYMENTS": [
        "PAYMENT_ID",
        "ORDER_ID",
        "PROVIDER",
        "STRIPE_CHECKOUT_SESSION_ID",
        "STRIPE_PAYMENT_INTENT_ID",
        "PAYMENT_METHOD",
        "AMOUNT",
        "PROCESSING_FEE",
        "DISPLAY_STATUS",
        "STRIPE_STATUS",
        "CREATED_AT",
        "PAID_AT",
        "LAST_ATTEMPT_AT",
        "FAILURE_REASON",
        "PAYMENT_PURPOSE",
        "ENTRY_ID",
        "REGISTRATION_ID",
        "PARENT_PAYMENT_ID",
        "CURRENCY",
        "ORIGINAL_ENTRY_FEE",
        "TARGET_ENTRY_FEE",
        "REASON",
        "REQUESTED_BY_USER_ID",
        "REQUESTED_BY_EMAIL",
        "REQUESTED_AT",
        "STRIPE_CHECKOUT_URL",
        "NOTIFICATION_EMAIL",
        "NOTIFICATION_SENT_AT",
    ],
    "WAIVERS": [
        "WAIVER_ID",
        "ORDER_ID",
        "COMPETITION_ID",
        "ORGANIZATION_ID",
        "SIGNED_BY_USER_ID",
        "SIGNED_BY_NAME",
        "WAIVER_VERSION",
        "SIGNED_AT",
    ],
    "REFUNDS": [
        "REFUND_ID",
        "PAYMENT_ID",
        "ORDER_ID",
        "ENTRY_ID",
        "REGISTRATION_ID",
        "REQUESTED_AMOUNT",
        "APPROVED_AMOUNT",
        "CURRENCY",
        "REASON",
        "REFUND_TYPE",
        "ORIGINAL_ENTRY_FEE",
        "TARGET_ENTRY_FEE",
        "REFUND_GROUP_ID",
        "REFUND_SEQUENCE",
        "REFUND_GROUP_TOTAL",
        "SOURCE_PAYMENT_PURPOSE",
        "STATUS",
        "REQUESTED_BY_USER_ID",
        "REQUESTED_BY_EMAIL",
        "REQUESTED_AT",
        "APPROVED_BY_USER_ID",
        "APPROVED_AT",
        "DECIDED_BY_USER_ID",
        "DECIDED_AT",
        "DECISION_REASON",
        "STRIPE_REFUND_ID",
        "STRIPE_STATUS",
        "COMPLETED_AT",
        "FAILURE_REASON",
        "UPDATED_AT",
    ],
    "AUDIT_LOG": [
        "AUDIT_ID",
        "TIMESTAMP",
        "USER_ID",
        "USER_EMAIL",
        "ACTION",
        "ENTITY_TYPE",
        "ENTITY_ID",
        "ORDER_ID",
        "BEFORE_JSON",
        "AFTER_JSON",
        "REASON",
    ],
}


ID_COLUMNS = {
    "ORDERS": "ORDER_ID",
    "REGISTRATIONS": "REGISTRATION_ID",
    "EVENT_ENTRIES": "ENTRY_ID",
    "PAYMENTS": "PAYMENT_ID",
    "WAIVERS": "WAIVER_ID",
    "REFUNDS": "REFUND_ID",
    "AUDIT_LOG": "AUDIT_ID",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise(value: Any) -> str:
    return _clean(value).upper()


def _sheet_value(value: Any):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class WorksheetInfo:
    worksheet: Any
    header_row: int
    headers: list[str]
    header_map: dict[str, int]


class TransactionSheetStore:
    """Idempotent pilot transaction persistence on Google Sheets.

    The store creates the five transaction worksheets when they are absent.
    Existing worksheets are preserved. Required columns are added to an
    existing header row if necessary.

    Google Sheets does not provide a multi-sheet transaction. Idempotent IDs
    make retries safe if a write is interrupted part-way through.
    """

    def __init__(self, google_client, sheet_url: str):
        self.google_client = google_client
        self.sheet_url = _clean(sheet_url)
        if not self.sheet_url:
            raise TransactionStoreError(
                "TRANSACTION_SHEET_URL/CONFIG_SHEET_URL is empty."
            )
        try:
            self.spreadsheet = google_client.open_by_url(self.sheet_url)
        except Exception as exc:
            raise TransactionStoreError(
                "Could not open the transaction Google Sheet: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._info_cache: dict[str, WorksheetInfo] = {}

    def ensure_schema(self) -> None:
        for sheet_name in SHEETS:
            self._worksheet_info(sheet_name)

    def _get_or_create_worksheet(self, sheet_name: str):
        try:
            worksheets = self.spreadsheet.worksheets()
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not list worksheets: {type(exc).__name__}: {exc}"
            ) from exc

        for worksheet in worksheets:
            if _clean(getattr(worksheet, "title", "")) == sheet_name:
                return worksheet

        headers = SHEETS[sheet_name]
        try:
            worksheet = self.spreadsheet.add_worksheet(
                title=sheet_name,
                rows=1000,
                cols=max(30, len(headers) + 5),
            )
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not create worksheet '{sheet_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        self._update_range(worksheet, "A1", [headers])
        return worksheet

    @staticmethod
    def _update_range(worksheet, range_name: str, values: list[list[Any]]) -> None:
        # gspread changed the preferred positional argument order in newer
        # releases. Support both forms so the pilot is not version-fragile.
        try:
            worksheet.update(values=values, range_name=range_name)
        except TypeError:
            worksheet.update(range_name, values)

    def _worksheet_info(self, sheet_name: str) -> WorksheetInfo:
        if sheet_name in self._info_cache:
            return self._info_cache[sheet_name]

        if sheet_name not in SHEETS:
            raise TransactionStoreError(f"Unknown transaction sheet: {sheet_name}")

        worksheet = self._get_or_create_worksheet(sheet_name)
        required = SHEETS[sheet_name]

        try:
            preview = worksheet.get("A1:AZ10")
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not inspect worksheet '{sheet_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        preview = preview or []
        header_row = None
        headers: list[str] = []

        required_set = {_normalise(x) for x in required}
        id_header = _normalise(required[0])

        for index, row in enumerate(preview, start=1):
            normalised = [_normalise(x) for x in row]
            if id_header in normalised and len(required_set.intersection(normalised)) >= 2:
                header_row = index
                headers = [_clean(x) for x in row]
                break

        if header_row is None:
            has_content = any(
                any(_clean(cell) for cell in row)
                for row in preview
            )
            if not has_content:
                header_row = 1
                headers = list(required)
                self._update_range(worksheet, "A1", [headers])
            else:
                # Accommodate a decorative title in A1 with blank row 2,
                # matching the pilot workbook style.
                first_row = preview[0] if preview else []
                remaining = preview[1:] if len(preview) > 1 else []
                only_title = (
                    len([x for x in first_row if _clean(x)]) == 1
                    and _normalise(first_row[0] if first_row else "") == sheet_name
                    and not any(any(_clean(x) for x in row) for row in remaining)
                )
                if only_title:
                    header_row = 3
                    headers = list(required)
                    self._update_range(worksheet, f"A{header_row}", [headers])
                else:
                    raise TransactionStoreError(
                        f"Worksheet '{sheet_name}' exists but its header row "
                        "could not be identified. Expected a row containing "
                        f"'{required[0]}'."
                    )

        # Add any new required columns without disturbing existing columns.
        header_map = {
            _normalise(header): idx
            for idx, header in enumerate(headers, start=1)
            if _clean(header)
        }
        missing = [h for h in required if _normalise(h) not in header_map]
        if missing:
            start_col = len(headers) + 1
            for offset, header in enumerate(missing):
                worksheet.update_cell(header_row, start_col + offset, header)
                headers.append(header)
            header_map = {
                _normalise(header): idx
                for idx, header in enumerate(headers, start=1)
                if _clean(header)
            }

        info = WorksheetInfo(
            worksheet=worksheet,
            header_row=header_row,
            headers=headers,
            header_map=header_map,
        )
        self._info_cache[sheet_name] = info
        return info

    def _existing_row_number(
        self,
        sheet_name: str,
        column_name: str,
        value: Any,
    ) -> int | None:
        info = self._worksheet_info(sheet_name)
        column_index = info.header_map.get(_normalise(column_name))
        if not column_index:
            raise TransactionStoreError(
                f"Column '{column_name}' is missing from '{sheet_name}'."
            )

        try:
            values = info.worksheet.col_values(column_index)
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not read '{column_name}' from '{sheet_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        target = _clean(value)
        for row_number, cell_value in enumerate(values, start=1):
            if row_number <= info.header_row:
                continue
            if _clean(cell_value) == target:
                return row_number
        return None

    def append_if_missing(self, sheet_name: str, row: dict[str, Any]) -> bool:
        info = self._worksheet_info(sheet_name)
        id_column = ID_COLUMNS[sheet_name]
        id_value = _clean(row.get(id_column))
        if not id_value:
            raise TransactionStoreError(
                f"{sheet_name} row is missing required ID '{id_column}'."
            )

        if self._existing_row_number(sheet_name, id_column, id_value):
            return False

        values = [
            _sheet_value(row.get(header, ""))
            for header in info.headers
        ]
        try:
            info.worksheet.append_row(
                values,
                value_input_option="USER_ENTERED",
            )
        except TypeError:
            info.worksheet.append_row(values)
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not append to '{sheet_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return True

    def append_many_if_missing(
        self,
        sheet_name: str,
        rows: list[dict[str, Any]],
    ) -> int:
        count = 0
        for row in rows:
            if self.append_if_missing(sheet_name, row):
                count += 1
        return count

    def update_by_id(
        self,
        sheet_name: str,
        id_value: Any,
        updates: dict[str, Any],
    ) -> None:
        info = self._worksheet_info(sheet_name)
        id_column = ID_COLUMNS[sheet_name]
        row_number = self._existing_row_number(
            sheet_name,
            id_column,
            id_value,
        )
        if row_number is None:
            raise TransactionStoreError(
                f"Could not find {sheet_name} row where "
                f"{id_column}={id_value!r}."
            )

        for column_name, value in updates.items():
            column_index = info.header_map.get(_normalise(column_name))
            if not column_index:
                # The store may be a cached Streamlit resource created before a
                # schema upgrade. Refresh this worksheet's header map once so
                # newly-required columns can be added and discovered without
                # forcing a full schema scan on every page rerun.
                self._info_cache.pop(sheet_name, None)
                info = self._worksheet_info(sheet_name)
                column_index = info.header_map.get(_normalise(column_name))
            if not column_index:
                raise TransactionStoreError(
                    f"Column '{column_name}' is missing from '{sheet_name}'."
                )
            info.worksheet.update_cell(
                row_number,
                column_index,
                _sheet_value(value),
            )

    def update_where(
        self,
        sheet_name: str,
        where_column: str,
        where_value: Any,
        updates: dict[str, Any],
    ) -> int:
        info = self._worksheet_info(sheet_name)
        where_index = info.header_map.get(_normalise(where_column))
        if not where_index:
            raise TransactionStoreError(
                f"Column '{where_column}' is missing from '{sheet_name}'."
            )

        try:
            values = info.worksheet.col_values(where_index)
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not scan '{sheet_name}.{where_column}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        target = _clean(where_value)
        row_numbers = [
            row_number
            for row_number, cell_value in enumerate(values, start=1)
            if row_number > info.header_row and _clean(cell_value) == target
        ]

        for row_number in row_numbers:
            for column_name, value in updates.items():
                column_index = info.header_map.get(_normalise(column_name))
                if not column_index:
                    self._info_cache.pop(sheet_name, None)
                    info = self._worksheet_info(sheet_name)
                    column_index = info.header_map.get(_normalise(column_name))
                if not column_index:
                    raise TransactionStoreError(
                        f"Column '{column_name}' is missing from '{sheet_name}'."
                    )
                info.worksheet.update_cell(
                    row_number,
                    column_index,
                    _sheet_value(value),
                )
        return len(row_numbers)

    def find_first(
        self,
        sheet_name: str,
        where_column: str,
        where_value: Any,
    ) -> dict[str, str] | None:
        info = self._worksheet_info(sheet_name)
        where_index = info.header_map.get(_normalise(where_column))
        if not where_index:
            raise TransactionStoreError(
                f"Column '{where_column}' is missing from '{sheet_name}'."
            )

        row_number = None
        try:
            values = info.worksheet.col_values(where_index)
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not scan '{sheet_name}.{where_column}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        target = _clean(where_value)
        for idx, cell_value in enumerate(values, start=1):
            if idx > info.header_row and _clean(cell_value) == target:
                row_number = idx
                break

        if row_number is None:
            return None

        try:
            row_values = info.worksheet.row_values(row_number)
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not read row {row_number} from '{sheet_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        result = {}
        for idx, header in enumerate(info.headers):
            result[header] = row_values[idx] if idx < len(row_values) else ""
        return result

    def list_rows(self, sheet_name: str) -> list[dict[str, str]]:
        """Return all data rows from a transaction worksheet as dictionaries."""
        info = self._worksheet_info(sheet_name)
        try:
            values = info.worksheet.get_all_values()
        except Exception as exc:
            raise TransactionStoreError(
                f"Could not read '{sheet_name}': {type(exc).__name__}: {exc}"
            ) from exc

        rows: list[dict[str, str]] = []
        for row_values in values[info.header_row:]:
            if not any(_clean(v) for v in row_values):
                continue
            row: dict[str, str] = {}
            for idx, header in enumerate(info.headers):
                row[header] = row_values[idx] if idx < len(row_values) else ""
            rows.append(row)
        return rows

    def append_audit_log(self, row: dict[str, Any]) -> bool:
        """Append one immutable audit record, idempotently by AUDIT_ID."""
        return self.append_if_missing("AUDIT_LOG", row)

    def create_refund_request(self, row: dict[str, Any]) -> bool:
        """Record a refund request only; this method never calls Stripe."""
        entry_id = _clean(row.get("ENTRY_ID"))
        if entry_id:
            existing = self.list_rows("REFUNDS")
            open_statuses = {"REFUND_REQUESTED", "REFUND_STARTED"}
            for existing_row in existing:
                if (
                    _clean(existing_row.get("ENTRY_ID")) == entry_id
                    and _normalise(existing_row.get("STATUS")) in open_statuses
                ):
                    raise TransactionStoreError(
                        f"Entry {entry_id} already has an open refund request."
                    )
        return self.append_if_missing("REFUNDS", row)

    def create_refund_group(self, rows: list[dict[str, Any]]) -> int:
        """Record a multi-payment refund group using idempotent child rows.

        Google Sheets does not provide a cross-row transaction. This method first
        validates that the entry has no other open refund, then appends each child
        allocation idempotently. Retrying with the same REFUND_ID values fills in
        only missing child rows.
        """
        if not rows:
            raise TransactionStoreError("Refund group contains no allocations.")

        entry_ids = {
            _clean(row.get("ENTRY_ID"))
            for row in rows
            if _clean(row.get("ENTRY_ID"))
        }
        if len(entry_ids) != 1:
            raise TransactionStoreError(
                "All refund-group allocations must belong to one EVENT_ENTRY."
            )
        entry_id = next(iter(entry_ids), "")

        group_ids = {
            _clean(row.get("REFUND_GROUP_ID"))
            for row in rows
            if _clean(row.get("REFUND_GROUP_ID"))
        }
        if len(group_ids) != 1:
            raise TransactionStoreError(
                "All refund-group allocations must share one REFUND_GROUP_ID."
            )

        existing = self.list_rows("REFUNDS")
        open_statuses = {
            "REFUND_REQUESTED",
            "REFUND_STARTED",
            "REFUND_FAILED",
        }
        incoming_ids = {_clean(row.get("REFUND_ID")) for row in rows}
        for existing_row in existing:
            if (
                _clean(existing_row.get("ENTRY_ID")) == entry_id
                and _normalise(existing_row.get("STATUS")) in open_statuses
                and _clean(existing_row.get("REFUND_ID")) not in incoming_ids
            ):
                raise TransactionStoreError(
                    f"Entry {entry_id} already has an open refund request."
                )

        return self.append_many_if_missing("REFUNDS", rows)


    def persist_order_bundle(
        self,
        *,
        order: dict[str, Any],
        registrations: list[dict[str, Any]],
        event_entries: list[dict[str, Any]],
        waiver: dict[str, Any],
        payment: dict[str, Any],
    ) -> None:
        """Persist a complete order idempotently.

        If a previous attempt stopped after one sheet, retrying this method with
        the same IDs fills in only the missing rows.
        """
        self.ensure_schema()
        self.append_if_missing("ORDERS", order)
        self.append_many_if_missing("REGISTRATIONS", registrations)
        self.append_many_if_missing("EVENT_ENTRIES", event_entries)
        self.append_if_missing("WAIVERS", waiver)
        self.append_if_missing("PAYMENTS", payment)

    def mark_payment_started(
        self,
        *,
        order_id: str,
        payment_id: str,
        stripe_session_id: str,
        attempted_at: str,
    ) -> None:
        self.update_by_id(
            "ORDERS",
            order_id,
            {
                "STATUS": "PAYMENT_STARTED",
                "UPDATED_AT": attempted_at,
            },
        )
        self.update_by_id(
            "PAYMENTS",
            payment_id,
            {
                "STRIPE_CHECKOUT_SESSION_ID": stripe_session_id,
                "DISPLAY_STATUS": "PAYMENT_STARTED",
                "STRIPE_STATUS": "checkout_created",
                "LAST_ATTEMPT_AT": attempted_at,
                "FAILURE_REASON": "",
            },
        )
        self.update_where(
            "EVENT_ENTRIES",
            "ORDER_ID",
            order_id,
            {
                "PAYMENT_STATUS": "PAYMENT_STARTED",
                "PAYMENT_STATUS_CHANGED_AT": attempted_at,
                "UPDATED_AT": attempted_at,
            },
        )

    def mark_payment_complete_by_stripe_session(
        self,
        *,
        stripe_session_id: str,
        stripe_payment_intent_id: str = "",
        paid_at: str,
        payment_method: str = "",
        processing_fee: Any = "",
    ) -> str:
        """Webhook helper: confirm all rows belonging to a paid order.

        Returns the ORDER_ID. This is designed to be called only after Stripe
        webhook verification, never from the browser return URL.
        """
        payment = self.find_first(
            "PAYMENTS",
            "STRIPE_CHECKOUT_SESSION_ID",
            stripe_session_id,
        )
        if not payment:
            raise TransactionStoreError(
                "No PAYMENTS row matches Stripe Checkout session "
                f"{stripe_session_id!r}."
            )

        payment_id = _clean(payment.get("PAYMENT_ID"))
        order_id = _clean(payment.get("ORDER_ID"))
        if not payment_id or not order_id:
            raise TransactionStoreError(
                "The matched PAYMENTS row is missing PAYMENT_ID or ORDER_ID."
            )

        payment_updates = {
            "STRIPE_PAYMENT_INTENT_ID": stripe_payment_intent_id,
            "PROCESSING_FEE": processing_fee,
            "DISPLAY_STATUS": "PAYMENT_COMPLETE",
            "STRIPE_STATUS": "paid",
            "PAID_AT": paid_at,
            "LAST_ATTEMPT_AT": paid_at,
            "FAILURE_REASON": "",
        }
        if str(payment_method or "").strip():
            payment_updates["PAYMENT_METHOD"] = str(payment_method).strip()

        self.update_by_id(
            "PAYMENTS",
            payment_id,
            payment_updates,
        )
        self.update_by_id(
            "ORDERS",
            order_id,
            {
                "STATUS": "CONFIRMED",
                "UPDATED_AT": paid_at,
            },
        )
        self.update_where(
            "REGISTRATIONS",
            "ORDER_ID",
            order_id,
            {
                "STATUS": "CONFIRMED",
                "UPDATED_AT": paid_at,
            },
        )
        self.update_where(
            "EVENT_ENTRIES",
            "ORDER_ID",
            order_id,
            {
                "PAYMENT_STATUS": "PAYMENT_COMPLETE",
                "PAYMENT_STATUS_CHANGED_AT": paid_at,
                "STATUS": "CONFIRMED",
                "UPDATED_AT": paid_at,
            },
        )
        return order_id

    def mark_payment_failed_by_stripe_session(
        self,
        *,
        stripe_session_id: str,
        failed_at: str,
        stripe_status: str,
        failure_reason: str,
    ) -> str:
        """Webhook helper that returns the stakeholder status to REQUIRED."""
        payment = self.find_first(
            "PAYMENTS",
            "STRIPE_CHECKOUT_SESSION_ID",
            stripe_session_id,
        )
        if not payment:
            raise TransactionStoreError(
                "No PAYMENTS row matches Stripe Checkout session "
                f"{stripe_session_id!r}."
            )

        payment_id = _clean(payment.get("PAYMENT_ID"))
        order_id = _clean(payment.get("ORDER_ID"))

        self.update_by_id(
            "PAYMENTS",
            payment_id,
            {
                "DISPLAY_STATUS": "REQUIRED",
                "STRIPE_STATUS": stripe_status,
                "LAST_ATTEMPT_AT": failed_at,
                "FAILURE_REASON": failure_reason,
            },
        )
        self.update_by_id(
            "ORDERS",
            order_id,
            {
                "STATUS": "PENDING_PAYMENT",
                "UPDATED_AT": failed_at,
            },
        )
        self.update_where(
            "EVENT_ENTRIES",
            "ORDER_ID",
            order_id,
            {
                "PAYMENT_STATUS": "REQUIRED",
                "PAYMENT_STATUS_CHANGED_AT": failed_at,
                "UPDATED_AT": failed_at,
            },
        )
        return order_id
