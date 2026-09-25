from __future__ import annotations

import datetime as dt
import json
import secrets
from decimal import Decimal

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.admin_output_projection import OutputAdminError, sync_output_entry
from signup.email import send_confirmation_email_smtp
from signup.moe_billing import (
    ACTIVE_INVOICE_STATUSES,
    active_invoice_entries,
    build_invoice_line_rows,
    group_invoice_lines,
    invoice_email_body,
    invoice_pdf_bytes,
    invoice_summary,
    invoice_xlsx_bytes,
)
from signup.multi_payment_transaction_store import TransactionSheetStore, TransactionStoreError
from signup.pilot_config import PilotConfigError, PilotConfigRepository, require_configured_user


st.set_page_config(page_title="SAA MOE Billing", layout="wide")
st.title("SAA MOE / vendor@gov Billing")
st.caption(
    "Phase 4B: issue school invoices from submitted active entry lines, export Excel/PDF, "
    "record disputes, and let SA Finance record payment received."
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
    store = TransactionSheetStore(gc, TRANSACTION_SHEET_URL)
    store.ensure_schema()
    return gc, store


try:
    google_client, store = _resources("phase4b-moe-billing-v1")
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


def _audit(*, action: str, entity_type: str, entity_id: str, order_id: str, before: dict, after: dict, reason: str) -> None:
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
    oid = _clean(payment.get("ORDER_ID"))
    if oid and _upper(payment.get("PROVIDER")) == "INVOICE":
        payment_by_order[oid] = payment

invoice_groups = group_invoice_lines(invoice_rows)
invoice_by_order: dict[str, tuple[str, list[dict]]] = {}
for invoice_id, lines in invoice_groups.items():
    if not lines:
        continue
    oid = _clean(lines[0].get("ORDER_ID"))
    if oid:
        invoice_by_order[oid] = (invoice_id, lines)

invoice_orders = []
for order in orders:
    order_id = _clean(order.get("ORDER_ID"))
    org = organization_by_id.get(_clean(order.get("ORGANIZATION_ID")), {})
    if _upper(order.get("PAYMENT_TYPE")) == "INVOICE" or order_id in payment_by_order:
        invoice_orders.append(order)
    elif org.get("type") == "SCHOOL":
        # Tolerate old pilot school rows whose PAYMENT_TYPE was not populated.
        invoice_orders.append(order)

if not invoice_orders:
    st.info("No school / invoice orders are currently available.")
    st.stop()

competition_ids = []
for order in invoice_orders:
    cid = _clean(order.get("COMPETITION_ID"))
    if cid and cid not in competition_ids:
        competition_ids.append(cid)

selected_competition = st.selectbox(
    "Competition",
    competition_ids,
    format_func=lambda cid: f"{competition_name_by_id.get(cid, cid)} ({cid})",
)

filtered_orders = [
    order for order in invoice_orders if _clean(order.get("COMPETITION_ID")) == selected_competition
]


def _order_label(order: dict) -> str:
    oid = _clean(order.get("ORDER_ID"))
    org = organization_by_id.get(_clean(order.get("ORGANIZATION_ID")), {})
    invoice = invoice_by_order.get(oid)
    status = "NOT INVOICED"
    if invoice:
        status = invoice_summary(invoice[1]).get("STATUS", "")
    return f"{org.get('name') or order.get('ORGANIZATION_ID')} | {oid} | {status}"

selected_order_id = st.selectbox(
    "School order",
    [_clean(order.get("ORDER_ID")) for order in filtered_orders],
    format_func=lambda oid: _order_label(next(o for o in filtered_orders if _clean(o.get("ORDER_ID")) == oid)),
)
selected_order = next(o for o in filtered_orders if _clean(o.get("ORDER_ID")) == selected_order_id)
org_info = organization_by_id.get(_clean(selected_order.get("ORGANIZATION_ID")), {})
submitter_email = email_by_user_id.get(_clean(selected_order.get("USER_ID")), "")
active_entries = active_invoice_entries(entries, selected_order_id)
existing_invoice_pair = invoice_by_order.get(selected_order_id)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Order total", f"SGD {Decimal(_clean(selected_order.get('TOTAL_AMOUNT')) or '0'):.2f}")
m2.metric("Submitted entries", int(sum(1 for e in entries if _clean(e.get("ORDER_ID")) == selected_order_id)))
m3.metric("Billable before invoice", len(active_entries))
m4.metric("Invoice status", invoice_summary(existing_invoice_pair[1]).get("STATUS", "NOT INVOICED") if existing_invoice_pair else "NOT INVOICED")

if not existing_invoice_pair:
    st.subheader("Issue invoice")
    st.caption(
        "The invoice snapshot includes submitted entries that are still active at issue time. "
        "Withdrawn/deleted entries are excluded, preserving the requirement that a school may cancel before invoicing."
    )
    if not active_entries:
        st.warning("This order has no active entries to invoice.")
    else:
        preview = pd.DataFrame(
            [
                {
                    "ATHLETE": _clean(e.get("ATHLETE_NAME")),
                    "EVENT": _clean(e.get("EVENT_NAME")),
                    "DIVISION": _clean(e.get("DIVISION")),
                    "PERIOD": _clean(e.get("REGISTRATION_PERIOD")),
                    "ENTRY_FEE": float(Decimal(_clean(e.get("ENTRY_FEE")) or "0")),
                    "ENTRY_ID": _clean(e.get("ENTRY_ID")),
                }
                for e in active_entries
            ]
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)
        invoice_total = sum((Decimal(_clean(e.get("ENTRY_FEE")) or "0") for e in active_entries), Decimal("0"))
        st.metric("Invoice amount", f"SGD {invoice_total:.2f}")

        issue_reason = st.text_input("Issue note / reason", value="Post-event MOE billing")
        confirm = st.checkbox("I confirm this invoice snapshot is ready for SA Finance / vendor@gov processing.")
        if st.button("Issue MOE invoice", type="primary", disabled=not confirm):
            now = _now()
            invoice_id = _new_id("INV")
            year = dt.datetime.now(dt.timezone.utc).year
            invoice_number = f"MOE-{year}-{invoice_id.split('-', 1)[1][:8]}"
            line_rows = build_invoice_line_rows(
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                order=selected_order,
                entries=active_entries,
                organization_name=org_info.get("name", ""),
                team_code=org_info.get("team_code", ""),
                created_at=now,
                created_by_user_id=user.user_id,
                created_by_email=user_email,
            )
            try:
                existing_for_order = [
                    row for row in store.list_rows("MOE_INVOICES")
                    if _clean(row.get("ORDER_ID")) == selected_order_id
                    and _upper(row.get("STATUS")) in ACTIVE_INVOICE_STATUSES
                ]
                if existing_for_order:
                    raise TransactionStoreError("This order already has an active MOE invoice.")
                written = store.append_many_if_missing("MOE_INVOICES", line_rows)
                if written != len(line_rows):
                    raise TransactionStoreError(
                        f"Expected to write {len(line_rows)} invoice lines but wrote {written}."
                    )
                payment = payment_by_order.get(selected_order_id)
                if payment:
                    store.update_by_id(
                        "PAYMENTS",
                        _clean(payment.get("PAYMENT_ID")),
                        {
                            "AMOUNT": f"{invoice_total:.2f}",
                            "CURRENCY": "SGD",
                            "STRIPE_STATUS": "invoice_issued",
                            "LAST_ATTEMPT_AT": now,
                        },
                    )
                _audit(
                    action="MOE_INVOICE_ISSUED",
                    entity_type="MOE_INVOICE",
                    entity_id=invoice_id,
                    order_id=selected_order_id,
                    before={},
                    after={
                        "INVOICE_NUMBER": invoice_number,
                        "ENTRY_COUNT": len(line_rows),
                        "AMOUNT": f"{invoice_total:.2f}",
                    },
                    reason=issue_reason,
                )
                if submitter_email:
                    summary = invoice_summary(line_rows)
                    try:
                        send_confirmation_email_smtp(
                            to_email=submitter_email,
                            subject=f"Singapore Athletics MOE invoice issued - {invoice_number}",
                            body=invoice_email_body(
                                summary=summary,
                                competition_name=competition_name_by_id.get(selected_competition, selected_competition),
                            ),
                        )
                        _audit(
                            action="MOE_INVOICE_EMAIL_SENT",
                            entity_type="MOE_INVOICE",
                            entity_id=invoice_id,
                            order_id=selected_order_id,
                            before={},
                            after={"EMAIL": submitter_email},
                            reason=issue_reason,
                        )
                    except Exception as email_exc:
                        _audit(
                            action="MOE_INVOICE_EMAIL_FAILED",
                            entity_type="MOE_INVOICE",
                            entity_id=invoice_id,
                            order_id=selected_order_id,
                            before={"EMAIL": submitter_email},
                            after={"ERROR": f"{type(email_exc).__name__}: {email_exc}"},
                            reason=issue_reason,
                        )
                        st.warning(f"Invoice was issued, but email failed: {type(email_exc).__name__}: {email_exc}")
                _read_rows.clear()
                st.success(f"Issued {invoice_number} for SGD {invoice_total:.2f}.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not issue MOE invoice: {type(exc).__name__}: {exc}")
else:
    invoice_id, lines = existing_invoice_pair
    summary = invoice_summary(lines)
    st.subheader(f"Invoice {summary.get('INVOICE_NUMBER', invoice_id)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", summary.get("STATUS", ""))
    c2.metric("Entries", summary.get("ENTRY_COUNT", 0))
    c3.metric("Amount", f"SGD {summary.get('AMOUNT', Decimal('0')):.2f}")
    c4.metric("Paid at", summary.get("PAID_AT") or "-")

    display = pd.DataFrame(
        [
            {
                "ATHLETE": _clean(line.get("ATHLETE_NAME")),
                "EVENT": _clean(line.get("EVENT_NAME")),
                "DIVISION": _clean(line.get("DIVISION")),
                "REGISTRATION_PERIOD": _clean(line.get("REGISTRATION_PERIOD")),
                "ENTRY_ID": _clean(line.get("ENTRY_ID")),
                "AMOUNT": float(Decimal(_clean(line.get("LINE_AMOUNT")) or "0")),
            }
            for line in lines
        ]
    )
    st.dataframe(display, use_container_width=True, hide_index=True)

    comp_name = competition_name_by_id.get(summary.get("COMPETITION_ID", ""), summary.get("COMPETITION_ID", ""))
    st.download_button(
        "Download invoice Excel",
        data=invoice_xlsx_bytes(invoice_lines=lines, competition_name=comp_name),
        file_name=f"{summary.get('INVOICE_NUMBER', invoice_id)}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.download_button(
        "Download invoice PDF",
        data=invoice_pdf_bytes(invoice_lines=lines, competition_name=comp_name),
        file_name=f"{summary.get('INVOICE_NUMBER', invoice_id)}.pdf",
        mime="application/pdf",
    )

    status = _upper(summary.get("STATUS"))
    if status in {"ISSUED", "DISPUTED"}:
        left, right = st.columns(2)
        with left:
            with st.form("mark_moe_paid"):
                payment_reference = st.text_input("Finance payment reference", placeholder="e.g. vendor@gov receipt / bank reference")
                confirm_paid = st.checkbox("I confirm SA Finance has received this invoice payment.")
                paid_submit = st.form_submit_button("Mark invoice paid", type="primary", disabled=not confirm_paid)
            if paid_submit:
                now = _now()
                payment = payment_by_order.get(selected_order_id)
                try:
                    if not payment:
                        raise TransactionStoreError("The school order has no invoice payment row.")
                    payment_id = _clean(payment.get("PAYMENT_ID"))
                    # Apply recoverable/idempotent child updates first. The invoice
                    # group is marked PAID last, so a partially interrupted click
                    # remains safely retryable.
                    store.update_by_id(
                        "PAYMENTS",
                        payment_id,
                        {
                            "AMOUNT": f"{summary['AMOUNT']:.2f}",
                            "CURRENCY": "SGD",
                            "DISPLAY_STATUS": "PAYMENT_COMPLETE",
                            "STRIPE_STATUS": "invoice_paid",
                            "PAYMENT_METHOD": "INVOICE",
                            "PAID_AT": now,
                            "LAST_ATTEMPT_AT": now,
                            "FAILURE_REASON": "",
                        },
                    )
                    invoice_entry_ids = {_clean(line.get("ENTRY_ID")) for line in lines}
                    matching_entries = [e for e in entries if _clean(e.get("ENTRY_ID")) in invoice_entry_ids]
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
                                st.warning(f"Transaction rows were updated, but OUTPUT projection update failed for {entry_id}: {output_exc}")
                    store.update_where(
                        "MOE_INVOICES",
                        "INVOICE_ID",
                        invoice_id,
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
                        action="MOE_INVOICE_PAID",
                        entity_type="MOE_INVOICE",
                        entity_id=invoice_id,
                        order_id=selected_order_id,
                        before={"STATUS": status},
                        after={
                            "STATUS": "PAID",
                            "AMOUNT": f"{summary['AMOUNT']:.2f}",
                            "PAYMENT_REFERENCE": payment_reference,
                        },
                        reason="SA Finance recorded payment received",
                    )
                    if submitter_email:
                        paid_summary = dict(summary)
                        paid_summary["STATUS"] = "PAID"
                        try:
                            send_confirmation_email_smtp(
                                to_email=submitter_email,
                                subject=f"Singapore Athletics MOE invoice paid - {summary.get('INVOICE_NUMBER', invoice_id)}",
                                body=invoice_email_body(summary=paid_summary, competition_name=comp_name, paid=True),
                            )
                            _audit(
                                action="MOE_INVOICE_PAID_EMAIL_SENT",
                                entity_type="MOE_INVOICE",
                                entity_id=invoice_id,
                                order_id=selected_order_id,
                                before={},
                                after={"EMAIL": submitter_email},
                                reason="Invoice payment receipt notice",
                            )
                        except Exception as email_exc:
                            _audit(
                                action="MOE_INVOICE_PAID_EMAIL_FAILED",
                                entity_type="MOE_INVOICE",
                                entity_id=invoice_id,
                                order_id=selected_order_id,
                                before={"EMAIL": submitter_email},
                                after={"ERROR": f"{type(email_exc).__name__}: {email_exc}"},
                                reason="Invoice payment receipt notice",
                            )
                            st.warning(f"Payment was recorded, but email failed: {type(email_exc).__name__}: {email_exc}")
                    _read_rows.clear()
                    st.success("Invoice payment recorded.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not mark invoice paid: {type(exc).__name__}: {exc}")

        with right:
            with st.form("flag_moe_dispute"):
                dispute_reason = st.text_area("Dispute reason", placeholder="Required to flag an invoice as disputed")
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
                            invoice_id,
                            {
                                "STATUS": "DISPUTED",
                                "DISPUTE_REASON": dispute_reason.strip(),
                                "UPDATED_AT": now,
                            },
                        )
                        _audit(
                            action="MOE_INVOICE_DISPUTED",
                            entity_type="MOE_INVOICE",
                            entity_id=invoice_id,
                            order_id=selected_order_id,
                            before={"STATUS": status},
                            after={"STATUS": "DISPUTED", "DISPUTE_REASON": dispute_reason.strip()},
                            reason=dispute_reason.strip(),
                        )
                        _read_rows.clear()
                        st.success("Invoice flagged as disputed for SA Finance / Events follow-up.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not flag invoice dispute: {type(exc).__name__}: {exc}")

    elif status == "PAID":
        st.success(
            f"SA Finance recorded this invoice as paid. Reference: {summary.get('PAYMENT_REFERENCE') or '-'}"
        )

st.divider()
st.caption(
    "Invoice lines are immutable snapshots of active submitted entries at issue time. "
    "Post-issue cancellations/credit notes are intentionally not automated in Phase 4B; "
    "flag the invoice as disputed for SA Finance / Events resolution."
)
