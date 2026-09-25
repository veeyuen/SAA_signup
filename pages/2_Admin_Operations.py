from __future__ import annotations

import datetime as dt
import json
import secrets
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.output_admin import OutputAdminError, sync_output_entry
from signup.refund_payment import StripeRefundError, create_stripe_refund
from signup.pilot_config import PilotConfigError, PilotConfigRepository, require_configured_user
from signup.transaction_store import TransactionSheetStore, TransactionStoreError


st.set_page_config(page_title="SAA Admin Operations", layout="wide")
st.title("SAA Admin Operations")
st.caption("Pilot admin tools for entry amendments, withdrawals, refund requests and audit history.")

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL) or CONFIG_SHEET_URL
).strip()
OUTPUT_SHEET_URL = str(
    st.secrets.get("OUTPUT_SHEET_URL", TRANSACTION_SHEET_URL) or TRANSACTION_SHEET_URL
).strip()
OUTPUT_WORKSHEET = str(st.secrets.get("OUTPUT_WORKSHEET", "OUTPUT") or "OUTPUT").strip()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)
user_email, user, organization = require_configured_user(
    repository=pilot_config,
    app_title="SAA Admin Operations",
    provider="auth0",
)
if user.role != "SAA_ADMIN":
    st.error("SAA_ADMIN access is required for this page.")
    st.stop()

@st.cache_resource(show_spinner=False)
def _admin_resources(schema_version: str):
    """Create the Google client/store once per Streamlit worker.

    The original Phase 3A page rebuilt the store and re-ran schema discovery on
    every Streamlit rerun. That is safe but very expensive in Google Sheets read
    quota. Keeping the resource alive lets TransactionSheetStore reuse its
    worksheet/header cache across widget reruns.
    """
    gc = create_google_client(dict(st.secrets["gcp_service_account"]))
    transaction_store = TransactionSheetStore(gc, TRANSACTION_SHEET_URL)
    transaction_store.ensure_schema()
    return gc, transaction_store


try:
    # Include the transaction schema generation in the cache key. This forces a
    # one-time resource refresh after schema-bearing deployments while retaining
    # the quota savings of cache_resource during normal widget reruns.
    google_client, store = _admin_resources("phase3b2-merged-hardening")
except Exception as exc:
    st.error(f"Could not initialise admin storage: {type(exc).__name__}: {exc}")
    st.stop()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()
    return f"{prefix}-{token}"


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _to_decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(_clean(value) or default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


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
            "BEFORE_JSON": json.dumps(before, ensure_ascii=False, sort_keys=True),
            "AFTER_JSON": json.dumps(after, ensure_ascii=False, sort_keys=True),
            "REASON": reason,
        }
    )


@st.cache_data(ttl=30, show_spinner=False)
def _load_admin_rows() -> dict[str, list[dict[str, str]]]:
    """Cache the admin read model briefly to avoid quota-heavy widget reruns."""
    return {
        name: store.list_rows(name)
        for name in (
            "ORDERS",
            "REGISTRATIONS",
            "EVENT_ENTRIES",
            "PAYMENTS",
            "REFUNDS",
            "AUDIT_LOG",
        )
    }


def _recompute_registration_status(
    registration_id: str,
    order_id: str,
    reason: str,
    *,
    withdrawn_entry_id: str,
    entries_snapshot: list[dict[str, str]],
    registrations_snapshot: list[dict[str, str]],
) -> None:
    # Use the already-loaded page snapshot instead of re-reading EVENT_ENTRIES
    # and REGISTRATIONS immediately after the withdrawal write.
    registration_entries = []
    for row in entries_snapshot:
        if _clean(row.get("REGISTRATION_ID")) != registration_id:
            continue
        effective = dict(row)
        if _clean(effective.get("ENTRY_ID")) == withdrawn_entry_id:
            effective["STATUS"] = "WITHDRAWN"
            effective["IS_DELETED"] = "TRUE"
        registration_entries.append(effective)

    if not registration_entries:
        return

    withdrawn = [
        row for row in registration_entries
        if _clean(row.get("STATUS")).upper() == "WITHDRAWN"
        or _clean(row.get("IS_DELETED")).upper() == "TRUE"
    ]
    new_status = (
        "WITHDRAWN" if len(withdrawn) == len(registration_entries)
        else "PARTIALLY_WITHDRAWN" if withdrawn
        else "CONFIRMED"
    )

    registration = next(
        (
            row for row in registrations_snapshot
            if _clean(row.get("REGISTRATION_ID")) == registration_id
        ),
        {},
    )
    old_status = _clean(registration.get("STATUS"))
    if old_status != new_status:
        store.update_by_id(
            "REGISTRATIONS",
            registration_id,
            {"STATUS": new_status, "UPDATED_AT": _now()},
        )
        _audit(
            action="REGISTRATION_STATUS_RECOMPUTED",
            entity_type="REGISTRATION",
            entity_id=registration_id,
            order_id=order_id,
            before={"STATUS": old_status},
            after={"STATUS": new_status},
            reason=reason,
        )


try:
    admin_rows = _load_admin_rows()
    orders = admin_rows["ORDERS"]
    registrations = admin_rows["REGISTRATIONS"]
    entries = admin_rows["EVENT_ENTRIES"]
    payments = admin_rows["PAYMENTS"]
    refunds = admin_rows["REFUNDS"]
    audit_rows = admin_rows["AUDIT_LOG"]
except TransactionStoreError as exc:
    st.error(str(exc))
    st.info(
        "If this is a Google Sheets 429 quota error, wait about a minute and "
        "refresh. The page now caches reads to prevent repeated quota bursts."
    )
    st.stop()

if not orders:
    st.info("There are no transaction orders to administer yet.")
    st.stop()

search = st.text_input(
    "Search orders",
    placeholder="Order ID, athlete, team, email or competition ID",
).strip().casefold()

if search:
    matching_order_ids = set()
    for order in orders:
        hay = " ".join(_clean(v) for v in order.values()).casefold()
        if search in hay:
            matching_order_ids.add(_clean(order.get("ORDER_ID")))
    for registration in registrations:
        hay = " ".join(_clean(v) for v in registration.values()).casefold()
        if search in hay:
            matching_order_ids.add(_clean(registration.get("ORDER_ID")))
    for entry in entries:
        hay = " ".join(_clean(v) for v in entry.values()).casefold()
        if search in hay:
            matching_order_ids.add(_clean(entry.get("ORDER_ID")))
    filtered_orders = [o for o in orders if _clean(o.get("ORDER_ID")) in matching_order_ids]
else:
    filtered_orders = orders

if not filtered_orders:
    st.warning("No matching orders found.")
    st.stop()

order_ids = [_clean(o.get("ORDER_ID")) for o in filtered_orders if _clean(o.get("ORDER_ID"))]
selected_order_id = st.selectbox("Order", order_ids)
order = next(o for o in orders if _clean(o.get("ORDER_ID")) == selected_order_id)
order_registrations = [r for r in registrations if _clean(r.get("ORDER_ID")) == selected_order_id]
order_entries = [e for e in entries if _clean(e.get("ORDER_ID")) == selected_order_id]
payment = next((p for p in payments if _clean(p.get("ORDER_ID")) == selected_order_id), {})
order_refunds = [r for r in refunds if _clean(r.get("ORDER_ID")) == selected_order_id]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Order status", _clean(order.get("STATUS")) or "-")
c2.metric("Entries", _clean(order.get("ENTRY_COUNT")) or str(len(order_entries)))
c3.metric("Total", f"SGD {_clean(order.get('TOTAL_AMOUNT')) or '0'}")
c4.metric("Payment", _clean(payment.get("DISPLAY_STATUS")) or _clean(order.get("PAYMENT_TYPE")) or "-")

with st.expander("Order / registration detail", expanded=False):
    st.dataframe(pd.DataFrame(order_registrations), use_container_width=True, hide_index=True)

if not order_entries:
    st.info("This order has no event entries.")
    st.stop()

entry_labels = {
    _clean(e.get("ENTRY_ID")): (
        f"{_clean(e.get('ATHLETE_NAME'))} — {_clean(e.get('EVENT_NAME'))} "
        f"({_clean(e.get('DIVISION'))}) — {_clean(e.get('STATUS'))}"
    )
    for e in order_entries
}
selected_entry_id = st.selectbox(
    "Event entry",
    options=list(entry_labels),
    format_func=lambda eid: entry_labels[eid],
)
entry = next(e for e in order_entries if _clean(e.get("ENTRY_ID")) == selected_entry_id)

st.dataframe(pd.DataFrame([entry]), use_container_width=True, hide_index=True)

st.subheader("Amend season best")
with st.form("amend_season_best"):
    amended_sb = st.text_input("Season Best", value=_clean(entry.get("SEASON_BEST")))
    amendment_reason = st.text_area("Reason for amendment", placeholder="Required for audit trail")
    amend_clicked = st.form_submit_button("Save amendment", type="primary")

if amend_clicked:
    if not amendment_reason.strip():
        st.error("A reason is required.")
    elif _clean(entry.get("STATUS")).upper() == "WITHDRAWN":
        st.error("Withdrawn entries cannot be amended.")
    else:
        before = dict(entry)
        timestamp = _now()
        store.update_by_id(
            "EVENT_ENTRIES",
            selected_entry_id,
            {"SEASON_BEST": amended_sb.strip(), "UPDATED_AT": timestamp},
        )
        output_rows = sync_output_entry(
            gc=google_client,
            output_sheet_url_or_id=OUTPUT_SHEET_URL,
            output_worksheet=OUTPUT_WORKSHEET,
            entry=entry,
            season_best=amended_sb.strip(),
        )
        after = dict(before)
        after["SEASON_BEST"] = amended_sb.strip()
        after["UPDATED_AT"] = timestamp
        _audit(
            action="SEASON_BEST_AMENDED",
            entity_type="EVENT_ENTRY",
            entity_id=selected_entry_id,
            order_id=selected_order_id,
            before=before,
            after=after,
            reason=amendment_reason.strip(),
        )
        _load_admin_rows.clear()
        st.success(f"Season Best updated. Compatibility OUTPUT rows updated: {output_rows}.")
        st.rerun()

st.subheader("Withdraw event entry")
entry_withdrawn = (
    _clean(entry.get("STATUS")).upper() == "WITHDRAWN"
    or _clean(entry.get("IS_DELETED")).upper() == "TRUE"
)

# A previous withdrawal may have completed the entry/output writes but failed
# while recomputing the parent registration if Google Sheets quota was reached.
# Detect that state from the already-loaded snapshot and offer a safe repair.
_current_registration_id = _clean(entry.get("REGISTRATION_ID"))
_current_registration = next(
    (
        row for row in registrations
        if _clean(row.get("REGISTRATION_ID")) == _current_registration_id
    ),
    {},
)
_current_registration_entries = [
    row for row in entries
    if _clean(row.get("REGISTRATION_ID")) == _current_registration_id
]
_current_withdrawn = [
    row for row in _current_registration_entries
    if _clean(row.get("STATUS")).upper() == "WITHDRAWN"
    or _clean(row.get("IS_DELETED")).upper() == "TRUE"
]
_expected_registration_status = (
    "WITHDRAWN"
    if _current_registration_entries
    and len(_current_withdrawn) == len(_current_registration_entries)
    else "PARTIALLY_WITHDRAWN"
    if _current_withdrawn
    else "CONFIRMED"
)
_actual_registration_status = _clean(_current_registration.get("STATUS"))

if (
    _current_registration_id
    and _actual_registration_status
    and _actual_registration_status != _expected_registration_status
):
    st.warning(
        "Registration status is out of sync with its event entries: "
        f"{_actual_registration_status} → {_expected_registration_status}. "
        "This can happen if a Google Sheets quota error interrupts a withdrawal."
    )
    if st.button("Repair registration status", type="secondary"):
        timestamp = _now()
        store.update_by_id(
            "REGISTRATIONS",
            _current_registration_id,
            {"STATUS": _expected_registration_status, "UPDATED_AT": timestamp},
        )
        _audit(
            action="REGISTRATION_STATUS_REPAIRED",
            entity_type="REGISTRATION",
            entity_id=_current_registration_id,
            order_id=selected_order_id,
            before={"STATUS": _actual_registration_status},
            after={"STATUS": _expected_registration_status},
            reason="Repair after interrupted admin operation",
        )
        _load_admin_rows.clear()
        st.success("Registration status repaired.")
        st.rerun()

with st.form("withdraw_entry"):
    withdraw_reason = st.text_area("Withdrawal reason", placeholder="Required")
    confirm_withdraw = st.checkbox("I confirm that this event entry should be withdrawn.")
    withdraw_clicked = st.form_submit_button(
        "Withdraw event entry",
        disabled=entry_withdrawn,
    )

if withdraw_clicked:
    if not withdraw_reason.strip():
        st.error("A withdrawal reason is required.")
    elif not confirm_withdraw:
        st.error("Please confirm the withdrawal.")
    else:
        before = dict(entry)
        timestamp = _now()
        store.update_by_id(
            "EVENT_ENTRIES",
            selected_entry_id,
            {
                "STATUS": "WITHDRAWN",
                "IS_DELETED": True,
                "UPDATED_AT": timestamp,
            },
        )
        output_rows = sync_output_entry(
            gc=google_client,
            output_sheet_url_or_id=OUTPUT_SHEET_URL,
            output_worksheet=OUTPUT_WORKSHEET,
            entry=entry,
            status="WITHDRAWN",
            is_deleted=True,
        )
        after = dict(before)
        after.update({"STATUS": "WITHDRAWN", "IS_DELETED": "TRUE", "UPDATED_AT": timestamp})
        _audit(
            action="EVENT_ENTRY_WITHDRAWN",
            entity_type="EVENT_ENTRY",
            entity_id=selected_entry_id,
            order_id=selected_order_id,
            before=before,
            after=after,
            reason=withdraw_reason.strip(),
        )
        _recompute_registration_status(
            _clean(entry.get("REGISTRATION_ID")),
            selected_order_id,
            withdraw_reason.strip(),
            withdrawn_entry_id=selected_entry_id,
            entries_snapshot=entries,
            registrations_snapshot=registrations,
        )
        _load_admin_rows.clear()
        st.success(f"Entry withdrawn. Compatibility OUTPUT rows updated: {output_rows}.")
        st.rerun()

st.subheader("Refunds")
st.info(
    "Refunds are controlled by SAA Admin. A withdrawn paid entry first gets a refund "
    "request. An admin can then reject it or approve an amount and send the refund to "
    "Stripe. Stripe webhook events remain authoritative for final completion."
)

payment_complete = _clean(payment.get("DISPLAY_STATUS")).upper() == "PAYMENT_COMPLETE"
is_stripe = _clean(order.get("PAYMENT_TYPE")).upper() == "STRIPE"
entry_withdrawn = (
    _clean(entry.get("STATUS")).upper() == "WITHDRAWN"
    or _clean(entry.get("IS_DELETED")).upper() == "TRUE"
)

entry_refunds = [
    r for r in order_refunds
    if _clean(r.get("ENTRY_ID")) == selected_entry_id
]
open_refund = next(
    (
        r for r in entry_refunds
        if _clean(r.get("STATUS")).upper() in {"REFUND_REQUESTED", "REFUND_STARTED"}
    ),
    None,
)

payment_amount = _to_decimal(payment.get("AMOUNT"))
entry_fee = _to_decimal(entry.get("ENTRY_FEE"))

# Only refunds already sent to Stripe or completed reduce the remaining amount.
committed_statuses = {"REFUND_STARTED", "REFUND_COMPLETE"}
payment_committed = sum(
    (
        _to_decimal(r.get("APPROVED_AMOUNT"))
        for r in refunds
        if _clean(r.get("PAYMENT_ID")) == _clean(payment.get("PAYMENT_ID"))
        and _clean(r.get("STATUS")).upper() in committed_statuses
    ),
    Decimal("0"),
)
entry_committed = sum(
    (
        _to_decimal(r.get("APPROVED_AMOUNT"))
        for r in entry_refunds
        if _clean(r.get("STATUS")).upper() in committed_statuses
    ),
    Decimal("0"),
)

payment_remaining = max(Decimal("0"), payment_amount - payment_committed)
entry_remaining = (
    max(Decimal("0"), entry_fee - entry_committed)
    if entry_fee > 0
    else payment_remaining
)
max_new_refund = min(payment_remaining, entry_remaining)

if open_refund:
    st.warning(
        f"Open refund: {_clean(open_refund.get('REFUND_ID'))} "
        f"({_clean(open_refund.get('STATUS'))})."
    )

# ---------------------------------------------------------------------------
# 1. Record a new refund request
# ---------------------------------------------------------------------------
if not is_stripe:
    st.caption("This order is not a Stripe-paid order; Stripe refunds do not apply.")
elif not payment_complete:
    st.caption("The original Stripe payment is not complete, so a refund cannot be requested.")
elif not entry_withdrawn:
    st.caption("Withdraw the event entry before recording a refund request.")
elif open_refund is None and max_new_refund <= 0:
    st.success("This entry has no remaining refundable entry fee.")
elif open_refund is None:
    default_amount = max_new_refund
    with st.form("refund_request"):
        requested_amount_text = st.text_input(
            "Requested refund amount (SGD)",
            value=f"{default_amount:.2f}",
        )
        refund_reason = st.text_area("Refund reason", placeholder="Required")
        request_clicked = st.form_submit_button("Record refund request")

    if request_clicked:
        amount = _to_decimal(requested_amount_text, "-1")
        if amount <= 0:
            st.error("Requested refund amount must be greater than zero.")
        elif amount > max_new_refund:
            st.error(
                "Requested refund amount exceeds the remaining refundable amount "
                f"of SGD {max_new_refund:.2f}."
            )
        elif not refund_reason.strip():
            st.error("A refund reason is required.")
        else:
            timestamp = _now()
            refund_id = _new_id("RFD")
            currency = pilot_config.system_value("CURRENCY", "SGD") or "SGD"
            refund_row = {
                "REFUND_ID": refund_id,
                "PAYMENT_ID": _clean(payment.get("PAYMENT_ID")),
                "ORDER_ID": selected_order_id,
                "ENTRY_ID": selected_entry_id,
                "REGISTRATION_ID": _clean(entry.get("REGISTRATION_ID")),
                "REQUESTED_AMOUNT": f"{amount:.2f}",
                "APPROVED_AMOUNT": "",
                "CURRENCY": currency.upper(),
                "REASON": refund_reason.strip(),
                "STATUS": "REFUND_REQUESTED",
                "REQUESTED_BY_USER_ID": user.user_id,
                "REQUESTED_BY_EMAIL": user_email,
                "REQUESTED_AT": timestamp,
                "APPROVED_BY_USER_ID": "",
                "APPROVED_AT": "",
                "DECIDED_BY_USER_ID": "",
                "DECIDED_AT": "",
                "DECISION_REASON": "",
                "STRIPE_REFUND_ID": "",
                "STRIPE_STATUS": "",
                "COMPLETED_AT": "",
                "FAILURE_REASON": "",
                "UPDATED_AT": timestamp,
            }
            store.create_refund_request(refund_row)
            store.update_by_id(
                "EVENT_ENTRIES",
                selected_entry_id,
                {
                    "PAYMENT_STATUS": "REFUND_REQUESTED",
                    "PAYMENT_STATUS_CHANGED_AT": timestamp,
                    "UPDATED_AT": timestamp,
                },
            )
            sync_output_entry(
                gc=google_client,
                output_sheet_url_or_id=OUTPUT_SHEET_URL,
                output_worksheet=OUTPUT_WORKSHEET,
                entry=entry,
                payment_status="REFUND_REQUESTED",
            )
            _audit(
                action="REFUND_REQUESTED",
                entity_type="REFUND",
                entity_id=refund_id,
                order_id=selected_order_id,
                before={},
                after=refund_row,
                reason=refund_reason.strip(),
            )
            _load_admin_rows.clear()
            st.success(f"Refund request {refund_id} recorded. No funds have been moved yet.")
            st.rerun()

# ---------------------------------------------------------------------------
# 2. Decide a pending refund request
# ---------------------------------------------------------------------------
if open_refund and _clean(open_refund.get("STATUS")).upper() == "REFUND_REQUESTED":
    st.markdown("#### Refund decision")
    requested_amount = _to_decimal(open_refund.get("REQUESTED_AMOUNT"))
    max_approvable = min(requested_amount, max_new_refund)
    payment_intent_id = _clean(payment.get("STRIPE_PAYMENT_INTENT_ID"))

    st.write(
        f"Requested: **SGD {requested_amount:.2f}**  |  "
        f"Maximum currently approvable: **SGD {max_approvable:.2f}**"
    )
    st.caption(
        "Only the competition entry amount is refundable here. Stripe's original processing "
        "fees are not added to the refund."
    )

    with st.form("refund_decision"):
        approved_amount_text = st.text_input(
            "Approved refund amount (SGD)",
            value=f"{max_approvable:.2f}",
        )
        decision_reason = st.text_area(
            "Decision note",
            placeholder="Required for rejection; optional for approval",
        )
        confirm_send = st.checkbox(
            "I confirm that approval will send this refund to Stripe.",
        )
        approve_clicked = st.form_submit_button(
            "Approve and send to Stripe",
            type="primary",
        )
        reject_clicked = st.form_submit_button("Reject refund request")

    if reject_clicked:
        if not decision_reason.strip():
            st.error("A rejection reason is required.")
        else:
            timestamp = _now()
            before = dict(open_refund)
            updates = {
                "STATUS": "REFUND_REJECTED",
                "DECIDED_BY_USER_ID": user.user_id,
                "DECIDED_AT": timestamp,
                "DECISION_REASON": decision_reason.strip(),
                "UPDATED_AT": timestamp,
            }
            store.update_by_id("REFUNDS", _clean(open_refund.get("REFUND_ID")), updates)
            store.update_by_id(
                "EVENT_ENTRIES",
                selected_entry_id,
                {
                    "PAYMENT_STATUS": "PAYMENT_COMPLETE",
                    "PAYMENT_STATUS_CHANGED_AT": timestamp,
                    "UPDATED_AT": timestamp,
                },
            )
            sync_output_entry(
                gc=google_client,
                output_sheet_url_or_id=OUTPUT_SHEET_URL,
                output_worksheet=OUTPUT_WORKSHEET,
                entry=entry,
                payment_status="PAYMENT_COMPLETE",
            )
            after = dict(before)
            after.update(updates)
            _audit(
                action="REFUND_REJECTED",
                entity_type="REFUND",
                entity_id=_clean(open_refund.get("REFUND_ID")),
                order_id=selected_order_id,
                before=before,
                after=after,
                reason=decision_reason.strip(),
            )
            _load_admin_rows.clear()
            st.success("Refund request rejected. No funds were moved.")
            st.rerun()

    if approve_clicked:
        approved_amount = _to_decimal(approved_amount_text, "-1")
        if approved_amount <= 0:
            st.error("Approved refund amount must be greater than zero.")
        elif approved_amount > requested_amount:
            st.error("Approved amount cannot exceed the requested amount.")
        elif approved_amount > max_approvable:
            st.error(
                "Approved amount exceeds the remaining refundable amount "
                f"of SGD {max_approvable:.2f}."
            )
        elif not confirm_send:
            st.error("Please confirm that the refund should be sent to Stripe.")
        elif not payment_intent_id:
            st.error("The payment does not contain a Stripe PaymentIntent ID.")
        else:
            timestamp = _now()
            stripe_secret_key = str(st.secrets.get("STRIPE_SECRET_KEY", "") or "").strip()
            refund_id = _clean(open_refund.get("REFUND_ID"))
            try:
                stripe_result = create_stripe_refund(
                    secret_key=stripe_secret_key,
                    refund_id=refund_id,
                    payment_intent_id=payment_intent_id,
                    amount=approved_amount,
                    currency=_clean(open_refund.get("CURRENCY")) or "SGD",
                    entry_id=selected_entry_id,
                    registration_id=_clean(entry.get("REGISTRATION_ID")),
                    order_id=selected_order_id,
                    payment_id=_clean(payment.get("PAYMENT_ID")),
                    approved_by_user_id=user.user_id,
                    approved_at=timestamp,
                )
            except StripeRefundError as exc:
                # Leave the request pending so an admin can safely retry. The
                # Stripe idempotency key protects against accidental duplicates.
                try:
                    store.update_by_id(
                        "REFUNDS",
                        refund_id,
                        {
                            "FAILURE_REASON": str(exc),
                            "UPDATED_AT": timestamp,
                        },
                    )
                except Exception:
                    pass
                st.error(f"Stripe refund was not started: {exc}")
            else:
                stripe_status = _clean(stripe_result.get("status")).lower()
                technical_failure = stripe_status in {"failed", "canceled"}
                immediate_success = stripe_status == "succeeded"
                refund_status = (
                    "REFUND_FAILED"
                    if technical_failure
                    else "REFUND_COMPLETE"
                    if immediate_success
                    else "REFUND_STARTED"
                )
                entry_payment_status = (
                    "PAYMENT_COMPLETE"
                    if technical_failure
                    else "REFUND_COMPLETE"
                    if immediate_success
                    else "REFUND_STARTED"
                )
                failure_reason = _clean(stripe_result.get("failure_reason"))

                before = dict(open_refund)
                updates = {
                    "APPROVED_AMOUNT": f"{approved_amount:.2f}",
                    "APPROVED_BY_USER_ID": user.user_id,
                    "APPROVED_AT": timestamp,
                    "DECIDED_BY_USER_ID": user.user_id,
                    "DECIDED_AT": timestamp,
                    "DECISION_REASON": decision_reason.strip(),
                    "STATUS": refund_status,
                    "STRIPE_REFUND_ID": _clean(stripe_result.get("refund_id")),
                    "STRIPE_STATUS": stripe_status,
                    "FAILURE_REASON": failure_reason,
                    "COMPLETED_AT": timestamp if immediate_success else "",
                    "UPDATED_AT": timestamp,
                }
                try:
                    store.update_by_id("REFUNDS", refund_id, updates)
                    store.update_by_id(
                        "EVENT_ENTRIES",
                        selected_entry_id,
                        {
                            "PAYMENT_STATUS": entry_payment_status,
                            "PAYMENT_STATUS_CHANGED_AT": timestamp,
                            "UPDATED_AT": timestamp,
                        },
                    )
                    sync_output_entry(
                        gc=google_client,
                        output_sheet_url_or_id=OUTPUT_SHEET_URL,
                        output_worksheet=OUTPUT_WORKSHEET,
                        entry=entry,
                        payment_status=entry_payment_status,
                    )
                    after = dict(before)
                    after.update(updates)
                    _audit(
                        action=(
                            "REFUND_EXECUTION_FAILED"
                            if technical_failure
                            else "REFUND_APPROVED_AND_COMPLETED"
                            if immediate_success
                            else "REFUND_APPROVED_AND_STARTED"
                        ),
                        entity_type="REFUND",
                        entity_id=refund_id,
                        order_id=selected_order_id,
                        before=before,
                        after=after,
                        reason=decision_reason.strip() or _clean(open_refund.get("REASON")),
                    )
                except Exception as exc:
                    st.error(
                        "Stripe accepted the refund, but local reconciliation was interrupted. "
                        "Do not create another refund request. The Stripe webhook can repair the "
                        f"state using refund {stripe_result.get('refund_id')}. Details: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    st.stop()

                _load_admin_rows.clear()
                if technical_failure:
                    st.error(
                        "Stripe returned a failed/canceled refund state. No completed refund was recorded."
                    )
                elif immediate_success:
                    st.success(
                        f"Refund {stripe_result.get('refund_id')} completed according to Stripe. "
                        "The webhook will reconcile and audit the final state as well."
                    )
                else:
                    st.success(
                        f"Refund {stripe_result.get('refund_id')} sent to Stripe. "
                        "Waiting for authoritative webhook reconciliation."
                    )
                st.rerun()

elif open_refund and _clean(open_refund.get("STATUS")).upper() == "REFUND_STARTED":
    st.info(
        f"Stripe refund {_clean(open_refund.get('STRIPE_REFUND_ID')) or '-'} is in progress. "
        f"Stripe status: {_clean(open_refund.get('STRIPE_STATUS')) or 'pending'}."
    )

with st.expander("Refund history", expanded=False):
    if order_refunds:
        st.dataframe(pd.DataFrame(order_refunds), use_container_width=True, hide_index=True)
    else:
        st.caption("No refund requests for this order.")

with st.expander("Audit log for this order", expanded=False):
    audits = [
        row for row in audit_rows
        if _clean(row.get("ORDER_ID")) == selected_order_id
    ]
    if audits:
        st.dataframe(pd.DataFrame(audits), use_container_width=True, hide_index=True)
    else:
        st.caption("No admin actions recorded for this order.")
