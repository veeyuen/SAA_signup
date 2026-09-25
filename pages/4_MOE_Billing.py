from __future__ import annotations

import datetime as dt
import json
import secrets
from collections import defaultdict
from decimal import Decimal

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.admin_output_projection import OutputAdminError, sync_output_entry
from signup.consolidated_moe_billing import (
    ACTIVE_INVOICE_STATUSES,
    active_invoiced_entry_ids,
    build_consolidated_invoice_line_rows,
    consolidated_invoice_entries,
    group_invoice_lines,
    format_singapore_timestamp,
    invoice_amount_by_order,
    invoice_email_body,
    invoice_pdf_bytes,
    invoice_summary,
    invoice_xlsx_bytes,
)
from signup.email import send_confirmation_email_smtp
from signup.moe_transaction_store import TransactionSheetStore, TransactionStoreError
from signup.pilot_config import PilotConfigError, PilotConfigRepository, require_configured_user


st.set_page_config(page_title="SAA MOE Billing", layout="wide")
st.title("SAA MOE / vendor@gov Billing")
st.caption(
    "Phase 4B.3: consolidated school invoicing by competition. One invoice can contain "
    "multiple orders, athletes, and event-entry lines for the same school."
)

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL) or CONFIG_SHEET_URL
).strip()
OUTPUT_SHEET_URL = str(st.secrets.get("OUTPUT_SHEET_URL", "") or "").strip()
OUTPUT_WORKSHEET = str(st.secrets.get("OUTPUT_WORKSHEET", "OUTPUT") or "OUTPUT").strip()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)
user_email, user, organization = require_configured_user(
    repository=pilot_config,
    app_title="SAA MOE Billing",
    provider="auth0",
)
if user.role != "SAA_ADMIN":
    st.error("SAA_ADMIN access is required for this page.")
    st.stop()


@st.cache_resource(show_spinner=False)
def _resources(schema_version: str):
    gc = create_google_client(dict(st.secrets["gcp_service_account"]))
    tx_store = TransactionSheetStore(gc, TRANSACTION_SHEET_URL)
    tx_store.ensure_schema()
    return gc, tx_store


try:
    google_client, store = _resources("phase4b4-cleanup-v1")
except Exception as exc:
    st.error(f"Could not initialise MOE billing storage: {type(exc).__name__}: {exc}")
    st.stop()


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _upper(value) -> str:
    return _clean(value).upper()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()
    return f"{prefix}-{token}"


def _audit(
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    order_id: str,
    before: dict,
    after: dict,
    reason: str,
) -> None:
    store.append_audit_log(
        {
            "AUDIT_ID": _new_id("AUD"),
            "TIMESTAMP": _now(),
            "USER_ID": user.user_id,
            "USER_EMAIL": user_email,
            "ACTION": action,
            "ENTITY_TYPE": entity_type,
            "ENTITY_ID": entity_id,
            "ORDER_ID": order_id,
            "BEFORE_JSON": json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
            "AFTER_JSON": json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
            "REASON": reason,
        }
    )


@st.cache_data(ttl=30, show_spinner=False)
def _read_rows():
    return {
        name: store.list_rows(name)
        for name in (
            "ORDERS",
            "REGISTRATIONS",
            "EVENT_ENTRIES",
            "PAYMENTS",
            "MOE_INVOICES",
        )
    }


try:
    data = _read_rows()
except TransactionStoreError as exc:
    st.error(str(exc))
    st.stop()

orders = data["ORDERS"]
entries = data["EVENT_ENTRIES"]
payments = data["PAYMENTS"]
invoice_rows = data["MOE_INVOICES"]
orders_by_id = {_clean(o.get("ORDER_ID")): o for o in orders if _clean(o.get("ORDER_ID"))}
entries_by_id = {_clean(e.get("ENTRY_ID")): e for e in entries if _clean(e.get("ENTRY_ID"))}

try:
    competitions = pilot_config.competitions(include_closed=True)
    organizations_df = pilot_config.table("ORGANIZATIONS")
    users_df = pilot_config.table("USERS")
except PilotConfigError as exc:
    st.error(str(exc))
    st.stop()

competition_name_by_id = {
    c.competition_id: c.competition_name or c.competition_id for c in competitions
}

organization_by_id: dict[str, dict] = {}
for _, row in organizations_df.iterrows():
    oid = _clean(row.get("ORGANIZATION_ID"))
    if oid:
        organization_by_id[oid] = {
            "name": _clean(row.get("ORGANIZATION_NAME")),
            "team_code": _clean(row.get("TEAM_CODE")),
            "type": _upper(row.get("ORGANIZATION_TYPE")),
        }

email_by_user_id = {
    _clean(row.get("USER_ID")): _clean(row.get("EMAIL"))
    for _, row in users_df.iterrows()
    if _clean(row.get("USER_ID"))
}

payment_by_order: dict[str, dict] = {}
for payment in payments:
    order_id = _clean(payment.get("ORDER_ID"))
    if order_id and _upper(payment.get("PROVIDER")) == "INVOICE":
        payment_by_order[order_id] = payment


def _order_has_billable_value(order: dict) -> bool:
    order_id = _clean(order.get("ORDER_ID"))
    try:
        if Decimal(_clean(order.get("TOTAL_AMOUNT")) or "0") > 0:
            return True
    except Exception:
        pass
    payment = payment_by_order.get(order_id, {})
    try:
        if Decimal(_clean(payment.get("AMOUNT")) or "0") > 0:
            return True
    except Exception:
        pass
    return any(
        _clean(entry.get("ORDER_ID")) == order_id
        and _upper(entry.get("STATUS")) not in {"WITHDRAWN", "CANCELLED"}
        and _upper(entry.get("IS_DELETED")) not in {"TRUE", "1", "YES", "Y"}
        and Decimal(_clean(entry.get("ENTRY_FEE")) or "0") > 0
        for entry in entries
    )


invoice_groups = group_invoice_lines(invoice_rows)

invoice_orders: list[dict] = []
for order in orders:
    order_id = _clean(order.get("ORDER_ID"))
    org = organization_by_id.get(_clean(order.get("ORGANIZATION_ID")), {})
    if not _order_has_billable_value(order):
        # Zero-value school registrations are NO_COST, not MOE receivables.
        continue
    if _upper(order.get("PAYMENT_TYPE")) == "INVOICE" or order_id in payment_by_order:
        invoice_orders.append(order)
    elif org.get("type") == "SCHOOL":
        # Tolerate old pilot school rows whose PAYMENT_TYPE was not populated.
        invoice_orders.append(order)

if not invoice_orders:
    st.info("No school / invoice orders are currently available.")
    st.stop()

competition_ids: list[str] = []
for order in invoice_orders:
    cid = _clean(order.get("COMPETITION_ID"))
    if cid and cid not in competition_ids:
        competition_ids.append(cid)

selected_competition = st.selectbox(
    "Competition",
    competition_ids,
    format_func=lambda cid: f"{competition_name_by_id.get(cid, cid)} ({cid})",
)

competition_orders = [
    order for order in invoice_orders
    if _clean(order.get("COMPETITION_ID")) == selected_competition
]

school_org_ids: list[str] = []
for order in competition_orders:
    oid = _clean(order.get("ORGANIZATION_ID"))
    if oid and oid not in school_org_ids:
        school_org_ids.append(oid)


def _school_label(org_id: str) -> str:
    org = organization_by_id.get(org_id, {})
    return f"{org.get('name') or org_id} ({org_id})"


selected_org_id = st.selectbox("School", school_org_ids, format_func=_school_label)
org_info = organization_by_id.get(selected_org_id, {})
selected_school_orders = [
    order for order in competition_orders
    if _clean(order.get("ORGANIZATION_ID")) == selected_org_id
]
selected_order_ids = {_clean(order.get("ORDER_ID")) for order in selected_school_orders}

school_entries = [e for e in entries if _clean(e.get("ORDER_ID")) in selected_order_ids]
eligible_entries = consolidated_invoice_entries(
    entries=entries,
    eligible_order_ids=selected_order_ids,
    invoice_rows=invoice_rows,
)
active_invoiced_ids = active_invoiced_entry_ids(invoice_rows)
selected_already_invoiced = [
    e for e in school_entries if _clean(e.get("ENTRY_ID")) in active_invoiced_ids
]

selected_invoice_groups: dict[str, list[dict]] = {}
for invoice_id, lines in invoice_groups.items():
    if not lines:
        continue
    first = lines[0]
    if (
        _clean(first.get("COMPETITION_ID")) == selected_competition
        and _clean(first.get("ORGANIZATION_ID")) == selected_org_id
    ):
        selected_invoice_groups[invoice_id] = lines

m1, m2, m3, m4 = st.columns(4)
m1.metric("School orders", len(selected_school_orders))
m2.metric("Submitted entries", len(school_entries))
m3.metric("Uninvoiced active entries", len(eligible_entries))
m4.metric("Existing invoices", len(selected_invoice_groups))

st.subheader("Issue consolidated invoice")
st.caption(
    "All active, uninvoiced event-entry lines for this school and competition are included. "
    "Entries already on an ISSUED, DISPUTED, or PAID invoice are excluded, so an issued "
    "invoice remains an immutable snapshot and the same entry cannot be billed twice."
)

if selected_already_invoiced:
    st.info(
        f"{len(selected_already_invoiced)} entry line(s) for this school are already on an active invoice "
        "and will not be included again."
    )

if not eligible_entries:
    st.info("There are no active uninvoiced entries for this school and competition.")
else:
    preview = pd.DataFrame(
        [
            {
                "ATHLETE": _clean(e.get("ATHLETE_NAME")),
                "EVENT": _clean(e.get("EVENT_NAME")),
                "DIVISION": _clean(e.get("DIVISION")),
                "PERIOD": _clean(e.get("REGISTRATION_PERIOD")),
                "ORDER_ID": _clean(e.get("ORDER_ID")),
                "ENTRY_FEE": float(Decimal(_clean(e.get("ENTRY_FEE")) or "0")),
                "ENTRY_ID": _clean(e.get("ENTRY_ID")),
            }
            for e in eligible_entries
        ]
    )
    st.dataframe(preview, use_container_width=True, hide_index=True)
    invoice_total = sum(
        (Decimal(_clean(e.get("ENTRY_FEE")) or "0") for e in eligible_entries),
        Decimal("0"),
    )
    contributing_order_ids = sorted({_clean(e.get("ORDER_ID")) for e in eligible_entries})
    c1, c2, c3 = st.columns(3)
    c1.metric("Orders included", len(contributing_order_ids))
    c2.metric("Entries included", len(eligible_entries))
    c3.metric("Invoice amount", f"SGD {invoice_total:.2f}")

    issue_reason = st.text_input(
        "Issue note / reason",
        value="Consolidated post-event MOE billing",
        key="consolidated_issue_reason",
    )
    confirm = st.checkbox(
        "I confirm this consolidated school invoice is ready for SA Finance / vendor@gov processing.",
        key="consolidated_issue_confirm",
    )

    if st.button("Issue consolidated MOE invoice", type="primary", disabled=not confirm):
        now = _now()
        invoice_id = _new_id("INV")
        year = dt.datetime.now(dt.timezone.utc).year
        invoice_number = f"MOE-{year}-{invoice_id.split('-', 1)[1][:8]}"
        line_rows = build_consolidated_invoice_line_rows(
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            orders_by_id=orders_by_id,
            entries=eligible_entries,
            organization_id=selected_org_id,
            organization_name=org_info.get("name", ""),
            team_code=org_info.get("team_code", ""),
            competition_id=selected_competition,
            created_at=now,
            created_by_user_id=user.user_id,
            created_by_email=user_email,
        )
        amount_by_order = invoice_amount_by_order(line_rows)

        try:
            # Re-read invoice lines immediately before the write to prevent a
            # double-bill if another admin issued an invoice after this page loaded.
            latest_invoice_rows = store.list_rows("MOE_INVOICES")
            latest_invoiced_ids = active_invoiced_entry_ids(latest_invoice_rows)
            clashes = [
                _clean(line.get("ENTRY_ID"))
                for line in line_rows
                if _clean(line.get("ENTRY_ID")) in latest_invoiced_ids
            ]
            if clashes:
                raise TransactionStoreError(
                    "One or more selected entries were invoiced by another action. "
                    f"Refresh and retry. Entry IDs: {', '.join(clashes)}"
                )

            missing_payment_orders = [
                order_id for order_id in amount_by_order
                if order_id not in payment_by_order
            ]
            if missing_payment_orders:
                raise TransactionStoreError(
                    "Invoice payment placeholder is missing for order(s): "
                    + ", ".join(missing_payment_orders)
                )

            written = store.append_many_if_missing("MOE_INVOICES", line_rows)
            if written != len(line_rows):
                raise TransactionStoreError(
                    f"Expected to write {len(line_rows)} invoice lines but wrote {written}."
                )

            # Each contributing order retains its own invoice payment placeholder.
            # Set that row to the amount from THIS consolidated invoice belonging
            # to that order; do not copy the consolidated total to every payment.
            for order_id, order_amount in amount_by_order.items():
                payment = payment_by_order[order_id]
                store.update_by_id(
                    "PAYMENTS",
                    _clean(payment.get("PAYMENT_ID")),
                    {
                        "AMOUNT": f"{order_amount:.2f}",
                        "CURRENCY": "SGD",
                        "STRIPE_STATUS": "invoice_issued",
                        "LAST_ATTEMPT_AT": now,
                    },
                )

            _audit(
                action="MOE_CONSOLIDATED_INVOICE_ISSUED",
                entity_type="MOE_INVOICE",
                entity_id=invoice_id,
                order_id="",
                before={},
                after={
                    "INVOICE_NUMBER": invoice_number,
                    "ORGANIZATION_ID": selected_org_id,
                    "ORDER_IDS": sorted(amount_by_order),
                    "ORDER_COUNT": len(amount_by_order),
                    "ENTRY_COUNT": len(line_rows),
                    "AMOUNT": f"{invoice_total:.2f}",
                },
                reason=issue_reason,
            )

            summary = invoice_summary(line_rows)
            recipient_emails = sorted(
                {
                    email_by_user_id.get(_clean(orders_by_id[order_id].get("USER_ID")), "")
                    for order_id in amount_by_order
                    if order_id in orders_by_id
                }
                - {""}
            )
            for recipient in recipient_emails:
                try:
                    send_confirmation_email_smtp(
                        to_email=recipient,
                        subject=f"Singapore Athletics consolidated MOE invoice issued - {invoice_number}",
                        body=invoice_email_body(
                            summary=summary,
                            competition_name=competition_name_by_id.get(
                                selected_competition, selected_competition
                            ),
                        ),
                    )
                    _audit(
                        action="MOE_CONSOLIDATED_INVOICE_EMAIL_SENT",
                        entity_type="MOE_INVOICE",
                        entity_id=invoice_id,
                        order_id="",
                        before={},
                        after={"EMAIL": recipient},
                        reason=issue_reason,
                    )
                except Exception as email_exc:
                    _audit(
                        action="MOE_CONSOLIDATED_INVOICE_EMAIL_FAILED",
                        entity_type="MOE_INVOICE",
                        entity_id=invoice_id,
                        order_id="",
                        before={"EMAIL": recipient},
                        after={"ERROR": f"{type(email_exc).__name__}: {email_exc}"},
                        reason=issue_reason,
                    )
                    st.warning(
                        f"Invoice was issued, but email to {recipient} failed: "
                        f"{type(email_exc).__name__}: {email_exc}"
                    )

            _read_rows.clear()
            st.success(
                f"Issued consolidated invoice {invoice_number} for SGD {invoice_total:.2f} "
                f"across {len(amount_by_order)} order(s)."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Could not issue consolidated MOE invoice: {type(exc).__name__}: {exc}")


st.divider()
st.subheader("Existing school invoices")

if not selected_invoice_groups:
    st.caption("No invoice has yet been issued for this school and competition.")
else:
    invoice_ids = sorted(
        selected_invoice_groups,
        key=lambda iid: _clean(selected_invoice_groups[iid][0].get("ISSUED_AT")),
        reverse=True,
    )

    def _invoice_label(invoice_id: str) -> str:
        summary = invoice_summary(selected_invoice_groups[invoice_id])
        return (
            f"{summary.get('INVOICE_NUMBER', invoice_id)} | {summary.get('STATUS', '')} | "
            f"{summary.get('ENTRY_COUNT', 0)} entries | {summary.get('ORDER_COUNT', 0)} orders | "
            f"SGD {summary.get('AMOUNT', Decimal('0')):.2f}"
        )

    selected_invoice_id = st.selectbox(
        "Invoice",
        invoice_ids,
        format_func=_invoice_label,
    )
    lines = selected_invoice_groups[selected_invoice_id]
    summary = invoice_summary(lines)
    amount_by_order = invoice_amount_by_order(lines)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Status", summary.get("STATUS", ""))
    c2.metric("Orders", summary.get("ORDER_COUNT", 0))
    c3.metric("Entries", summary.get("ENTRY_COUNT", 0))
    c4.metric("Amount", f"SGD {summary.get('AMOUNT', Decimal('0')):.2f}")
    c5.metric("Paid at", format_singapore_timestamp(summary.get("PAID_AT")) or "-")

    display = pd.DataFrame(
        [
            {
                "ATHLETE": _clean(line.get("ATHLETE_NAME")),
                "EVENT": _clean(line.get("EVENT_NAME")),
                "DIVISION": _clean(line.get("DIVISION")),
                "REGISTRATION_PERIOD": _clean(line.get("REGISTRATION_PERIOD")),
                "ORDER_ID": _clean(line.get("ORDER_ID")),
                "ENTRY_ID": _clean(line.get("ENTRY_ID")),
                "AMOUNT": float(Decimal(_clean(line.get("LINE_AMOUNT")) or "0")),
            }
            for line in lines
        ]
    )
    st.dataframe(display, use_container_width=True, hide_index=True)

    comp_name = competition_name_by_id.get(
        summary.get("COMPETITION_ID", ""), summary.get("COMPETITION_ID", "")
    )
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "Download invoice Excel",
            data=invoice_xlsx_bytes(invoice_lines=lines, competition_name=comp_name),
            file_name=f"{summary.get('INVOICE_NUMBER', selected_invoice_id)}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with d2:
        st.download_button(
            "Download invoice PDF",
            data=invoice_pdf_bytes(invoice_lines=lines, competition_name=comp_name),
            file_name=f"{summary.get('INVOICE_NUMBER', selected_invoice_id)}.pdf",
            mime="application/pdf",
        )

    status = _upper(summary.get("STATUS"))
    if status in {"ISSUED", "DISPUTED"}:
        left, right = st.columns(2)
        with left:
            payment_reference = st.text_input(
                "Finance payment reference",
                placeholder="e.g. vendor@gov receipt / bank reference",
                key=f"moe_payment_reference_{selected_invoice_id}",
            )
            confirm_paid = st.checkbox(
                "I confirm SA Finance has received the full consolidated invoice payment.",
                key=f"moe_confirm_paid_{selected_invoice_id}",
            )
            paid_submit = st.button(
                "Mark invoice paid",
                type="primary",
                disabled=not (confirm_paid and bool(_clean(payment_reference))),
                key=f"mark_moe_paid_{selected_invoice_id}",
            )
            st.caption(
                "Enter a Finance payment reference and confirm receipt to enable this button."
            )

            if paid_submit:
                now = _now()
                invoice_entry_ids = {
                    _clean(line.get("ENTRY_ID")) for line in lines if _clean(line.get("ENTRY_ID"))
                }
                matching_entries = [
                    e for e in entries if _clean(e.get("ENTRY_ID")) in invoice_entry_ids
                ]
                try:
                    missing_payment_orders = [
                        order_id for order_id in amount_by_order
                        if order_id not in payment_by_order
                    ]
                    if missing_payment_orders:
                        raise TransactionStoreError(
                            "The invoice payment row is missing for order(s): "
                            + ", ".join(missing_payment_orders)
                        )

                    # Apply recoverable/idempotent child updates first. Mark the
                    # invoice PAID last, so an interrupted click remains retryable.
                    for order_id, order_amount in amount_by_order.items():
                        payment = payment_by_order[order_id]
                        store.update_by_id(
                            "PAYMENTS",
                            _clean(payment.get("PAYMENT_ID")),
                            {
                                "AMOUNT": f"{order_amount:.2f}",
                                "CURRENCY": "SGD",
                                "DISPLAY_STATUS": "PAYMENT_COMPLETE",
                                "STRIPE_STATUS": "invoice_paid",
                                "PAYMENT_METHOD": "INVOICE",
                                "PAID_AT": now,
                                "LAST_ATTEMPT_AT": now,
                                "FAILURE_REASON": "",
                            },
                        )

                    for entry in matching_entries:
                        entry_id = _clean(entry.get("ENTRY_ID"))
                        store.update_by_id(
                            "EVENT_ENTRIES",
                            entry_id,
                            {
                                "PAYMENT_STATUS": "PAYMENT_COMPLETE",
                                "PAYMENT_STATUS_CHANGED_AT": now,
                                "UPDATED_AT": now,
                            },
                        )
                        if OUTPUT_SHEET_URL:
                            try:
                                sync_output_entry(
                                    gc=google_client,
                                    output_sheet_url_or_id=OUTPUT_SHEET_URL,
                                    output_worksheet=OUTPUT_WORKSHEET,
                                    entry=entry,
                                    payment_status="PAYMENT_COMPLETE",
                                )
                            except OutputAdminError as output_exc:
                                st.warning(
                                    "Transaction rows were updated, but OUTPUT projection update "
                                    f"failed for {entry_id}: {output_exc}"
                                )

                    store.update_where(
                        "MOE_INVOICES",
                        "INVOICE_ID",
                        selected_invoice_id,
                        {
                            "STATUS": "PAID",
                            "PAID_AT": now,
                            "PAID_BY_USER_ID": user.user_id,
                            "PAYMENT_REFERENCE": payment_reference,
                            "DISPUTE_REASON": "",
                            "UPDATED_AT": now,
                        },
                    )
                    _audit(
                        action="MOE_CONSOLIDATED_INVOICE_PAID",
                        entity_type="MOE_INVOICE",
                        entity_id=selected_invoice_id,
                        order_id="",
                        before={"STATUS": status},
                        after={
                            "STATUS": "PAID",
                            "ORDER_IDS": sorted(amount_by_order),
                            "AMOUNT": f"{summary['AMOUNT']:.2f}",
                            "PAYMENT_REFERENCE": payment_reference,
                        },
                        reason="SA Finance recorded consolidated invoice payment received",
                    )

                    recipient_emails = sorted(
                        {
                            email_by_user_id.get(
                                _clean(orders_by_id[order_id].get("USER_ID")), ""
                            )
                            for order_id in amount_by_order
                            if order_id in orders_by_id
                        }
                        - {""}
                    )
                    paid_summary = dict(summary)
                    paid_summary["STATUS"] = "PAID"
                    for recipient in recipient_emails:
                        try:
                            send_confirmation_email_smtp(
                                to_email=recipient,
                                subject=(
                                    "Singapore Athletics consolidated MOE invoice paid - "
                                    f"{summary.get('INVOICE_NUMBER', selected_invoice_id)}"
                                ),
                                body=invoice_email_body(
                                    summary=paid_summary,
                                    competition_name=comp_name,
                                    paid=True,
                                ),
                            )
                            _audit(
                                action="MOE_CONSOLIDATED_INVOICE_PAID_EMAIL_SENT",
                                entity_type="MOE_INVOICE",
                                entity_id=selected_invoice_id,
                                order_id="",
                                before={},
                                after={"EMAIL": recipient},
                                reason="Invoice payment receipt notice",
                            )
                        except Exception as email_exc:
                            _audit(
                                action="MOE_CONSOLIDATED_INVOICE_PAID_EMAIL_FAILED",
                                entity_type="MOE_INVOICE",
                                entity_id=selected_invoice_id,
                                order_id="",
                                before={"EMAIL": recipient},
                                after={
                                    "ERROR": f"{type(email_exc).__name__}: {email_exc}"
                                },
                                reason="Invoice payment receipt notice",
                            )
                            st.warning(
                                f"Payment was recorded, but email to {recipient} failed: "
                                f"{type(email_exc).__name__}: {email_exc}"
                            )

                    _read_rows.clear()
                    st.success("Consolidated invoice payment recorded.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not mark invoice paid: {type(exc).__name__}: {exc}")

        with right:
            with st.form(f"flag_moe_dispute_{selected_invoice_id}"):
                dispute_reason = st.text_area(
                    "Dispute reason",
                    placeholder="Required to flag an invoice as disputed",
                )
                dispute_submit = st.form_submit_button("Flag invoice as disputed")
            if dispute_submit:
                if not dispute_reason.strip():
                    st.error("A dispute reason is required.")
                else:
                    now = _now()
                    try:
                        store.update_where(
                            "MOE_INVOICES",
                            "INVOICE_ID",
                            selected_invoice_id,
                            {
                                "STATUS": "DISPUTED",
                                "DISPUTE_REASON": dispute_reason.strip(),
                                "UPDATED_AT": now,
                            },
                        )
                        _audit(
                            action="MOE_CONSOLIDATED_INVOICE_DISPUTED",
                            entity_type="MOE_INVOICE",
                            entity_id=selected_invoice_id,
                            order_id="",
                            before={"STATUS": status},
                            after={
                                "STATUS": "DISPUTED",
                                "ORDER_IDS": sorted(amount_by_order),
                                "DISPUTE_REASON": dispute_reason.strip(),
                            },
                            reason=dispute_reason.strip(),
                        )
                        _read_rows.clear()
                        st.success(
                            "Invoice flagged as disputed for SA Finance / Events follow-up."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not flag invoice dispute: {type(exc).__name__}: {exc}")

    elif status == "PAID":
        st.success(
            "SA Finance recorded this invoice as paid. "
            f"Reference: {summary.get('PAYMENT_REFERENCE') or '-'}"
        )

st.divider()
st.caption(
    "Consolidation scope is one school + one competition. Invoice lines remain immutable snapshots. "
    "Later school submissions are picked up by a later consolidated invoice; previously invoiced "
    "entries are never silently added to or removed from an issued invoice."
)
