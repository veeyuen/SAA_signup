from __future__ import annotations

import datetime as dt
import io
import json
import secrets
from decimal import Decimal

import pandas as pd
import streamlit as st

from payment_store import create_google_client
from signup.financial_reporting import (
    add_team_subtotals,
    build_entry_financial_report,
    build_order_financial_summary,
    build_transaction_ledger,
)
from signup.multi_payment_transaction_store import TransactionSheetStore, TransactionStoreError
from signup.pilot_config import PilotConfigError, PilotConfigRepository, require_configured_user
from signup.stripe_financials import (
    StripeFinancialsError,
    get_payment_intent_financials,
    get_refund_financials,
)


st.set_page_config(page_title="SAA Financial Reporting", layout="wide")
st.title("SAA Financial Reporting")
st.caption(
    "Phase 4: payment/refund/invoice ledger reporting using completed transaction records "
    "and Stripe's actual balance-transaction fees/net settlement."
)

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL) or CONFIG_SHEET_URL
).strip()

pilot_config = PilotConfigRepository(CONFIG_SHEET_URL)
user_email, user, organization = require_configured_user(
    repository=pilot_config,
    app_title="SAA Financial Reporting",
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
    google_client, store = _resources("phase4b-moe-invoice-reporting-v1")
except Exception as exc:
    st.error(f"Could not initialise financial reporting storage: {type(exc).__name__}: {exc}")
    st.stop()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()
    return f"{prefix}-{token}"


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _audit_financial_backfill(*, entity_type: str, entity_id: str, order_id: str, before: dict, after: dict) -> None:
    store.append_audit_log(
        {
            "AUDIT_ID": _new_id("AUD"),
            "TIMESTAMP": _now(),
            "USER_ID": user.user_id,
            "USER_EMAIL": user_email,
            "ACTION": "STRIPE_FINANCIALS_BACKFILLED",
            "ENTITY_TYPE": entity_type,
            "ENTITY_ID": entity_id,
            "ORDER_ID": order_id,
            "BEFORE_JSON": json.dumps(before, ensure_ascii=False, sort_keys=True),
            "AFTER_JSON": json.dumps(after, ensure_ascii=False, sort_keys=True),
            "REASON": "Phase 4A actual Stripe balance-transaction reconciliation",
        }
    )


@st.cache_data(ttl=30, show_spinner=False)
def _read_rows() -> dict[str, list[dict[str, str]]]:
    return {
        name: store.list_rows(name)
        for name in (
            "ORDERS",
            "REGISTRATIONS",
            "EVENT_ENTRIES",
            "PAYMENTS",
            "REFUNDS",
            "MOE_INVOICES",
        )
    }


try:
    data = _read_rows()
except TransactionStoreError as exc:
    st.error(str(exc))
    st.stop()

orders = data["ORDERS"]
registrations = data["REGISTRATIONS"]
entries = data["EVENT_ENTRIES"]
payments = data["PAYMENTS"]
refunds = data["REFUNDS"]
invoices = data["MOE_INVOICES"]

try:
    competitions = pilot_config.competitions(include_closed=True)
except PilotConfigError as exc:
    st.error(str(exc))
    st.stop()

competition_name_by_id = {
    c.competition_id: c.competition_name or c.competition_id
    for c in competitions
}
competition_year_by_id = {
    c.competition_id: int(c.competition_start_at.year)
    for c in competitions
    if c.competition_start_at is not None
}

known_competition_ids = []
for order in orders:
    cid = _clean(order.get("COMPETITION_ID"))
    if cid and cid not in known_competition_ids:
        known_competition_ids.append(cid)

competition_options = ["ALL"] + known_competition_ids
selected_competition = st.selectbox(
    "Competition",
    competition_options,
    format_func=lambda cid: (
        "All competitions"
        if cid == "ALL"
        else f"{competition_name_by_id.get(cid, cid)} ({cid})"
    ),
)

if selected_competition == "ALL":
    selected_orders = orders
else:
    selected_orders = [
        o for o in orders
        if _clean(o.get("COMPETITION_ID")) == selected_competition
    ]
selected_order_ids = {_clean(o.get("ORDER_ID")) for o in selected_orders}
selected_entries = [e for e in entries if _clean(e.get("ORDER_ID")) in selected_order_ids]
selected_payments = [p for p in payments if _clean(p.get("ORDER_ID")) in selected_order_ids]
selected_refunds = [r for r in refunds if _clean(r.get("ORDER_ID")) in selected_order_ids]
selected_invoices = [i for i in invoices if _clean(i.get("ORDER_ID")) in selected_order_ids]

completed_payments_missing_actuals = [
    p for p in selected_payments
    if _clean(p.get("DISPLAY_STATUS")).upper() == "PAYMENT_COMPLETE"
    and _clean(p.get("STRIPE_PAYMENT_INTENT_ID"))
    and (
        not _clean(p.get("STRIPE_BALANCE_TRANSACTION_ID"))
        or _clean(p.get("STRIPE_FEE_ACTUAL")) == ""
        or _clean(p.get("STRIPE_NET_ACTUAL")) == ""
    )
]
completed_refunds_missing_actuals = [
    r for r in selected_refunds
    if _clean(r.get("STATUS")).upper() == "REFUND_COMPLETE"
    and _clean(r.get("STRIPE_REFUND_ID"))
    and (
        not _clean(r.get("STRIPE_BALANCE_TRANSACTION_ID"))
        or _clean(r.get("STRIPE_FEE_ACTUAL")) == ""
        or _clean(r.get("STRIPE_NET_ACTUAL")) == ""
    )
]

with st.expander("Stripe financial reconciliation", expanded=bool(completed_payments_missing_actuals or completed_refunds_missing_actuals)):
    st.write(
        "The report treats blank Stripe fee/net fields as unknown rather than zero. "
        "Use this one-time/backfill action to retrieve the actual Stripe balance transaction "
        "for completed payments and refunds. Future webhook events populate these fields automatically."
    )
    st.caption(
        f"Missing actuals in current filter: {len(completed_payments_missing_actuals)} payment(s), "
        f"{len(completed_refunds_missing_actuals)} refund(s)."
    )
    backfill_clicked = st.button(
        "Backfill missing Stripe fees / net settlement",
        type="primary",
        disabled=not (completed_payments_missing_actuals or completed_refunds_missing_actuals),
    )

    if backfill_clicked:
        secret_key = str(st.secrets.get("STRIPE_SECRET_KEY", "") or "").strip()
        if not secret_key:
            st.error("STRIPE_SECRET_KEY is missing from Streamlit secrets.")
        else:
            successes = 0
            failures: list[str] = []
            progress = st.progress(0)
            items = [
                ("PAYMENT", row) for row in completed_payments_missing_actuals
            ] + [
                ("REFUND", row) for row in completed_refunds_missing_actuals
            ]

            for index, (kind, row) in enumerate(items, start=1):
                try:
                    if kind == "PAYMENT":
                        result = get_payment_intent_financials(
                            secret_key=secret_key,
                            payment_intent_id=_clean(row.get("STRIPE_PAYMENT_INTENT_ID")),
                        )
                        entity_id = _clean(row.get("PAYMENT_ID"))
                        updates = {
                            "STRIPE_BALANCE_TRANSACTION_ID": result.balance_transaction_id,
                            "STRIPE_FEE_ACTUAL": f"{result.fee:.2f}",
                            "STRIPE_NET_ACTUAL": f"{result.net:.2f}",
                        }
                        before = {
                            key: _clean(row.get(key))
                            for key in updates
                        }
                        store.update_by_id("PAYMENTS", entity_id, updates)
                        _audit_financial_backfill(
                            entity_type="PAYMENT",
                            entity_id=entity_id,
                            order_id=_clean(row.get("ORDER_ID")),
                            before=before,
                            after=updates,
                        )
                    else:
                        result = get_refund_financials(
                            secret_key=secret_key,
                            stripe_refund_id=_clean(row.get("STRIPE_REFUND_ID")),
                        )
                        entity_id = _clean(row.get("REFUND_ID"))
                        updates = {
                            "STRIPE_BALANCE_TRANSACTION_ID": result.balance_transaction_id,
                            "STRIPE_FEE_ACTUAL": f"{result.fee:.2f}",
                            "STRIPE_NET_ACTUAL": f"{result.net:.2f}",
                        }
                        before = {
                            key: _clean(row.get(key))
                            for key in updates
                        }
                        store.update_by_id("REFUNDS", entity_id, updates)
                        _audit_financial_backfill(
                            entity_type="REFUND",
                            entity_id=entity_id,
                            order_id=_clean(row.get("ORDER_ID")),
                            before=before,
                            after=updates,
                        )
                    successes += 1
                except (StripeFinancialsError, TransactionStoreError, Exception) as exc:
                    failures.append(
                        f"{kind} {_clean(row.get('PAYMENT_ID' if kind == 'PAYMENT' else 'REFUND_ID'))}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                progress.progress(index / max(1, len(items)))

            _read_rows.clear()
            if failures:
                st.warning(
                    f"Backfilled {successes} Stripe object(s); {len(failures)} failed. "
                    "Successful rows were retained."
                )
                for failure in failures:
                    st.code(failure)
            else:
                st.success(f"Backfilled {successes} Stripe object(s).")
                st.rerun()

entry_report = build_entry_financial_report(
    entries=selected_entries,
    payments=selected_payments,
    refunds=selected_refunds,
    invoices=selected_invoices,
    competition_year_by_id=competition_year_by_id,
)
order_summary = build_order_financial_summary(
    orders=selected_orders,
    payments=selected_payments,
    refunds=selected_refunds,
    invoices=selected_invoices,
)
ledger = build_transaction_ledger(
    orders=selected_orders,
    payments=selected_payments,
    refunds=selected_refunds,
)

successful_payment_total = Decimal("0")
refund_total = Decimal("0")
for p in selected_payments:
    if _clean(p.get("DISPLAY_STATUS")).upper() == "PAYMENT_COMPLETE":
        successful_payment_total += Decimal(_clean(p.get("AMOUNT")) or "0")
for r in selected_refunds:
    if _clean(r.get("STATUS")).upper() == "REFUND_COMPLETE":
        refund_total += Decimal(_clean(r.get("APPROVED_AMOUNT")) or "0")

actual_rows = [
    row for row in selected_payments
    if _clean(row.get("DISPLAY_STATUS")).upper() == "PAYMENT_COMPLETE"
    and (
        _clean(row.get("PROVIDER")).upper() == "STRIPE"
        or _clean(row.get("STRIPE_PAYMENT_INTENT_ID"))
    )
] + [
    row for row in selected_refunds
    if _clean(row.get("STATUS")).upper() == "REFUND_COMPLETE"
    and _clean(row.get("STRIPE_REFUND_ID"))
]
actual_complete = bool(actual_rows) and all(
    _clean(row.get("STRIPE_FEE_ACTUAL")) != ""
    and _clean(row.get("STRIPE_NET_ACTUAL")) != ""
    for row in actual_rows
)
stripe_fee_total = sum(
    (Decimal(_clean(row.get("STRIPE_FEE_ACTUAL")) or "0") for row in actual_rows),
    Decimal("0"),
)
stripe_net_total = sum(
    (Decimal(_clean(row.get("STRIPE_NET_ACTUAL")) or "0") for row in actual_rows),
    Decimal("0"),
)

invoiced_total = sum(
    (Decimal(_clean(row.get("LINE_AMOUNT")) or "0") for row in selected_invoices
     if _clean(row.get("STATUS")).upper() in {"ISSUED", "DISPUTED", "PAID"}),
    Decimal("0"),
)

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Successful payments", f"SGD {successful_payment_total:.2f}")
m2.metric("Amount invoiced", f"SGD {invoiced_total:.2f}")
m3.metric("Completed refunds", f"SGD {refund_total:.2f}")
m4.metric("Customer net collected", f"SGD {(successful_payment_total - refund_total):.2f}")
m5.metric("Actual Stripe fees", f"SGD {stripe_fee_total:.2f}" if (actual_complete or not actual_rows) else "Incomplete")
m6.metric("Net after Stripe", f"SGD {stripe_net_total:.2f}" if (actual_complete or not actual_rows) else "Incomplete")

if actual_rows and not actual_complete:
    st.warning(
        "Actual Stripe financial coverage is incomplete. Revenue values that depend on "
        "Stripe fees remain blank until the missing balance transactions are backfilled."
    )

if entry_report.empty:
    st.info("No event-entry transaction data is available for this competition filter.")
else:
    unique_teams = entry_report["TEAM"].replace("", pd.NA).dropna().nunique()
    athlete_keys = (
        entry_report["REGISTRATION_ID"].replace("", pd.NA).fillna(entry_report["ATHLETE_NAME"])
    )
    unique_athletes = athlete_keys.nunique()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Teams", int(unique_teams))
    c2.metric("Athletes", int(unique_athletes))
    c3.metric("Event entries", int(len(entry_report)))
    c4.metric(
        "Withdrawn entries",
        int(entry_report["ENTRY_STATUS"].astype(str).str.upper().eq("WITHDRAWN").sum()),
    )

    st.subheader("Team / athlete financial report")
    st.caption(
        "Gross is the current approved ENTRY_FEE after amendments. Amount Paid includes the "
        "reconstructed original entry allocation plus completed fee-increase payments. Refunds "
        "include completed fee decreases and withdrawals. Collected = Amount Paid − Refunds. "
        "Revenue = Collected − actual Stripe fees allocated to the entry. Amount Invoiced comes "
        "from the immutable MOE invoice line snapshot when a school invoice is issued."
    )

    display_cols = [
        "TEAM",
        "ATHLETE_NAME",
        "COUNTRY",
        "GENDER",
        "AGE",
        "DIVISION",
        "EVENT",
        "GROSS",
        "AMOUNT_PAID",
        "AMOUNT_INVOICED",
        "COLLECTED",
        "REFUNDS",
        "STRIPE_FEES",
        "REVENUE",
        "ENTRY_STATUS",
        "PAYMENT_STATUS",
        "ENTRY_ID",
    ]
    subtotal_report = add_team_subtotals(entry_report)[display_cols]
    st.dataframe(
        subtotal_report,
        use_container_width=True,
        hide_index=True,
        column_config={
            col: st.column_config.NumberColumn(col.replace("_", " ").title(), format="%.2f")
            for col in [
                "GROSS",
                "AMOUNT_PAID",
                "AMOUNT_INVOICED",
                "COLLECTED",
                "REFUNDS",
                "STRIPE_FEES",
                "REVENUE",
            ]
        },
    )

    csv_bytes = subtotal_report.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download financial report CSV",
        data=csv_bytes,
        file_name=(
            "SAA_financial_report_all.csv"
            if selected_competition == "ALL"
            else f"SAA_financial_report_{selected_competition}.csv"
        ),
        mime="text/csv",
    )

    st.subheader("Registration counts")
    count_source = entry_report.copy()
    event_counts = (
        count_source.groupby(["EVENT", "DIVISION"], dropna=False)
        .size()
        .reset_index(name="ENTRY_COUNT")
        .sort_values(["EVENT", "DIVISION"])
    )
    gender_counts = (
        count_source.groupby(["TEAM", "GENDER"], dropna=False)["REGISTRATION_ID"]
        .nunique()
        .reset_index(name="ATHLETE_COUNT")
        .sort_values(["TEAM", "GENDER"])
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**Entries by event / division**")
        st.dataframe(event_counts, use_container_width=True, hide_index=True)
    with right:
        st.markdown("**Athletes by team / gender**")
        st.dataframe(gender_counts, use_container_width=True, hide_index=True)

with st.expander("Order financial summary", expanded=False):
    if order_summary.empty:
        st.caption("No completed order financial activity in this filter.")
    else:
        st.dataframe(order_summary, use_container_width=True, hide_index=True)

with st.expander("Immutable payment / refund ledger", expanded=False):
    if ledger.empty:
        st.caption("No completed payment/refund ledger activity in this filter.")
    else:
        st.dataframe(ledger, use_container_width=True, hide_index=True)

st.caption(
    "Financial reporting is read-only apart from the explicit Stripe-financial backfill action. "
    "It does not change payment amounts, refund amounts, entry fees or registration statuses."
)
