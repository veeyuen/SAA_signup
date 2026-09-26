# Streamlit Athlete Signup App (Dynamic dependent dropdowns)
# Generated on: 2026-03-01T11:27:08
#
# Key fix:
# - Removed st.form wrapper around dependent widgets so Event dropdown updates immediately
#   when Gender/Division changes.
#
# This app does NOT load the original Excel workbook.
# TEST
import re
import traceback as tb

import datetime as dt
import pandas as pd
import streamlit as st

from google_sheets_reader import read_sheet_as_df
from google_sheets_roster import load_roster, parse_dob, last4_from_nric
from google_sheets_writer import sync_entries_to_sheet
from reference_lists import (
    COUNTRIES,
    ENTRY_HEADERS,
    TEAM_CODES,
    get_events,
    get_team_name,
)

from signup.config import (
    APP_TITLE,
    DEFAULT_APP_TITLE,
    DIVISIONS,
    DIVISIONS_60M,
    SPRINT_60M_APP_TITLE,
    SPRINT_60M_ONLY_MODE,
)
from signup.email import (
    build_confirmation_email_html,
    send_confirmation_email_smtp,
)
from signup.events import (
    active_divisions as _active_divisions,
    allowed_events,
    coerce_division_key_for_options as _coerce_division_key_for_options,
    default_division_key as _default_division_key,
    division_display_label as _division_display_label,
    division_value_for_storage as _division_value_for_storage,
    event_sort_key as _event_sort_key,
)
from signup.export import (
    build_semicolon_export_from_output_sheet,
    export_entries_to_excel,
    sheet_df_to_entries as _sheet_df_to_entries,
)
from signup.formatting import (
    code_to_gender_display,
    compute_unique_id,
    gender_to_code,
    normalize_header as _normalize_header,
    safe_date_max,
)
from signup.session import apply_pending_text_updates, init_sheet_session
from signup.pilot_config import (
    PilotConfigError,
    PilotConfigRepository,
    clear_config_cache,
    require_configured_user,
)
from signup.cart import (
    add_item as add_cart_item,
    cart_competition_id,
    cart_has_items,
    clear_cart,
    flatten_entry_rows as cart_entry_rows,
    get_cart,
    remove_item as remove_cart_item,
    total_amount as cart_total_amount,
    total_event_entries as cart_total_event_entries,
)
from signup.transaction_store import (
    TransactionSheetStore,
    TransactionStoreError,
)
from signup.athlete_integrity import (
    candidate_from_cart_item,
    check_candidate_against_existing,
    normalise_person_name,
)
from signup.waiver_compliance import (
    WaiverComplianceError,
    build_waiver_record,
    normalise_signer_name,
    validate_waiver_acknowledgement,
)
from signup.competition_rules import validate_cart_competition_rules
from signup.validation import (
    is_valid_email,
    is_valid_ic_last4,
    match_option_case_insensitive as _match_option_case_insensitive,
    normalize_email,
    normalize_ic_last4,
)

import json
import secrets
from decimal import Decimal

from payment_store import (
    create_google_client,
    get_pending_worksheet,
)
from signup.pending_payment import (
    cancel_pending_registration,
    retarget_pending_checkout_session,
    upsert_pending_registration,
)
from signup.stripe_payment import (
    expire_registration_checkout,
    get_or_create_registration_checkout,
)
from signup.payment_recovery import (
    checkout_force_new,
    find_resumable_payment_orders,
    order_can_be_resumed,
    pending_stripe_order_in_scope,
)

APP_VARIANT = "public_entry_only"

LOGIN_REQUIRED_FOR_THIS_ROLLOUT = True

# ---------------- UI ----------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

CONFIG_SHEET_URL = str(st.secrets.get("CONFIG_SHEET_URL", "") or "").strip()
if not CONFIG_SHEET_URL:
    st.error("CONFIG_SHEET_URL is missing from Streamlit secrets.")
    st.stop()

TRANSACTION_SHEET_URL = str(
    st.secrets.get("TRANSACTION_SHEET_URL", CONFIG_SHEET_URL)
    or CONFIG_SHEET_URL
).strip()


def _transaction_store() -> TransactionSheetStore:
    try:
        google_client = create_google_client(
            dict(st.secrets["gcp_service_account"])
        )
    except Exception as exc:
        raise TransactionStoreError(
            "Could not create the Google Sheets service-account client: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return TransactionSheetStore(
        google_client=google_client,
        sheet_url=TRANSACTION_SHEET_URL,
    )


@st.cache_data(ttl=30, show_spinner=False)
def _cached_resumable_payment_orders(
    transaction_sheet_url: str,
    user_id: str,
    organization_id: str,
) -> list[dict]:
    # transaction_sheet_url participates in the cache key even though the store
    # reads the same configured URL. The short TTL cuts repeated Sheets reads
    # from Streamlit reruns without making payment state stale for long.
    _ = transaction_sheet_url
    store = _transaction_store()
    orders = store.list_rows("ORDERS")
    scoped_orders = [
        order
        for order in orders
        if pending_stripe_order_in_scope(
            order,
            user_id=user_id,
            organization_id=organization_id,
        )
    ]
    if not scoped_orders:
        return []

    return find_resumable_payment_orders(
        orders=scoped_orders,
        payments=store.list_rows("PAYMENTS"),
        registrations=store.list_rows("REGISTRATIONS"),
        event_entries=store.list_rows("EVENT_ENTRIES"),
        user_id=user_id,
        organization_id=organization_id,
    )

@st.cache_resource(show_spinner=False)
def _get_pilot_config_repository(sheet_url: str) -> PilotConfigRepository:
    # Keep the repository object alive across Streamlit widget reruns. Master
    # worksheet data itself is cached in signup.pilot_config.
    return PilotConfigRepository(sheet_url)


pilot_config = _get_pilot_config_repository(CONFIG_SHEET_URL)

if not LOGIN_REQUIRED_FOR_THIS_ROLLOUT:
    st.error("The Google Sheets pilot configuration requires login to be enabled.")
    st.stop()

current_user_email, current_user, current_organization = require_configured_user(
    repository=pilot_config,
    app_title=APP_TITLE,
    provider="auth0",
)

# Configuration changes are rare, so master tables are cached for quota safety.
# SAA admins can explicitly invalidate that cache after changing a master sheet.
if current_user.role == "SAA_ADMIN":
    with st.sidebar:
        if st.button("Refresh configuration", key="refresh_master_configuration"):
            clear_config_cache()
            st.toast("Configuration cache cleared. Reloading master data...")
            st.rerun()

st.title(APP_TITLE)


def _queue_clear_athlete_fields() -> None:
    """Queue athlete-specific widgets to reset safely on the next rerun."""
    values = {
        "last_name": "",
        "first_name": "",
        "other_name": "",
        "gender": "",
        "name_passport": "",
        "full_name": "",
        "db_name_override": "",
        "full_name_signature": "",
        "athlete_roster_match": "(keep typed)",
        "unique_id_override": "",
        "nationality": "",
        "nationality_override": "",
        "singapore_pr": False,
        "birth_date": None,
        "ic_last4": "",
        "contact_number": "",
        "email": "",
        "events_selected": [],
        "emergency_contact_name": "",
        "emergency_contact_number": "",
        "coach_full_name": "",
        "parq": "Y",
    }
    for key, value in values.items():
        st.session_state[f"{key}__pending"] = value

    for key in list(st.session_state.keys()):
        if str(key).startswith("season_best_event__"):
            st.session_state[f"{key}__pending"] = ""


def _start_another_registration() -> None:
    """Reset order/athlete state while keeping the user logged in and competition selected."""
    clear_cart()
    st.session_state.pop("pending_checkout", None)
    st.session_state.pop("order_waiver_ok", None)
    st.session_state.pop("order_waiver_signer_name", None)
    st.session_state.pop("draft_order_id", None)
    _queue_clear_athlete_fields()
    st.query_params.clear()
    st.rerun()


def show_stripe_return_status():
    """Show a safe return screen after Stripe redirects back to Streamlit.

    The redirect itself is not treated as proof of payment. The Stripe webhook
    remains responsible for saving the confirmed registration and sending the
    acknowledgement email.
    """
    payment_result = str(st.query_params.get("payment_result", "") or "").strip().lower()
    if not payment_result:
        return

    pending_checkout = st.session_state.get("pending_checkout", {}) or {}
    order_id = str(
        pending_checkout.get("order_id", "")
        or pending_checkout.get("registration_id", "")
        or ""
    ).strip()

    if payment_result == "cancelled":
        st.warning("Payment was cancelled. Your registration has not been confirmed.")
        if order_id:
            st.caption(f"Order reference: {order_id}")

        if st.button("Return to registration form", type="primary"):
            st.session_state.pop("pending_checkout", None)
            st.query_params.clear()
            st.rerun()

        st.stop()

    if payment_result == "success":
        session_id = str(st.query_params.get("session_id", "") or "").strip()

        st.success("Your payment has been submitted to Stripe.")
        st.info(
            "Your registration will be saved and the acknowledgement email will be "
            "sent only after Stripe's webhook confirms that the payment succeeded."
        )

        if order_id:
            st.write(f"Order reference: `{order_id}`")
        if session_id:
            st.caption(f"Stripe Checkout session: {session_id}")

        st.caption(
            "You can return to the registration screen and enter another athlete "
            "for the same competition."
        )
        if st.button(
            "← Back to registration",
            type="primary",
            key="back_to_registration_after_stripe",
        ):
            _start_another_registration()

        st.stop()


show_stripe_return_status()


def _fresh_resumable_candidate(order_id: str) -> tuple[TransactionSheetStore, dict | None]:
    """Re-read Sheets and return this user's still-resumable logical order."""
    store = _transaction_store()
    candidates = find_resumable_payment_orders(
        orders=store.list_rows("ORDERS"),
        payments=store.list_rows("PAYMENTS"),
        registrations=store.list_rows("REGISTRATIONS"),
        event_entries=store.list_rows("EVENT_ENTRIES"),
        user_id=current_user.user_id,
        organization_id=current_organization.organization_id,
    )
    wanted = str(order_id or "").strip()
    return store, next(
        (row for row in candidates if str(row.get("order_id", "")).strip() == wanted),
        None,
    )


def _cancel_persisted_payment(order_id: str) -> None:
    """Safely cancel one unpaid registration while preserving its history."""
    order_id = str(order_id or "").strip()
    if not order_id:
        st.error("The pending order reference is missing.")
        return

    stripe_secret_key = str(st.secrets.get("STRIPE_SECRET_KEY", "") or "").strip()
    if not stripe_secret_key:
        st.error("Stripe is not configured: STRIPE_SECRET_KEY is missing.")
        return

    try:
        store, candidate = _fresh_resumable_candidate(order_id)
    except Exception as exc:
        st.error(
            "Unable to verify the pending registration before cancellation: "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if not candidate:
        _cached_resumable_payment_orders.clear()
        st.info(
            "This order is no longer an unpaid resumable registration. "
            "Its status may already have changed."
        )
        return

    payment_id = str(candidate.get("payment_id", "") or "").strip()
    session_id = str(candidate.get("stripe_session_id", "") or "").strip()
    if not payment_id or not session_id:
        st.error(
            "This pending transaction is missing its payment/session reference and "
            "must be reviewed by SAA Admin rather than cancelled automatically."
        )
        return


    # Stripe is checked first and an open Checkout Session is expired before any
    # local row is cancelled. This closes the race where a user could otherwise
    # pay a Checkout URL immediately after cancelling the registration.
    try:
        stripe_result = expire_registration_checkout(
            secret_key=stripe_secret_key,
            session_id=session_id,
        )
    except Exception as exc:
        st.error(
            "Stripe could not verify/expire this Checkout session, so no local "
            "records were cancelled. "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if not stripe_result.get("can_cancel"):
        _cached_resumable_payment_orders.clear()
        st.warning(
            "Stripe reports that this Checkout session has already completed or "
            "been paid. The registration was not cancelled; refresh after the "
            "payment webhook finishes processing."
        )
        return

    cancelled_at = dt.datetime.now(dt.timezone.utc).isoformat()

    # Keep the legacy PendingPayments record synchronized. It is historical
    # storage used by the webhook, so rows are retained and status is changed
    # rather than physically deleted.
    try:
        pending_sheet_url = str(
            st.secrets.get("PENDING_PAYMENT_SHEET_URL", "") or ""
        ).strip()
        pending_worksheet_name = str(
            st.secrets.get("PENDING_PAYMENT_WORKSHEET", "PendingPayments")
            or "PendingPayments"
        ).strip()
        if not pending_sheet_url:
            raise RuntimeError("PENDING_PAYMENT_SHEET_URL is missing.")
        google_client = create_google_client(dict(st.secrets["gcp_service_account"]))
        pending_worksheet = get_pending_worksheet(
            google_client, pending_sheet_url, pending_worksheet_name
        )
        cancel_pending_registration(
            worksheet=pending_worksheet,
            registration_id=order_id,
        )
    except Exception as exc:
        st.error(
            "Stripe Checkout was expired, but PendingPayments could not be updated. "
            "The transaction rows were left unchanged so the cancellation can be "
            "retried safely. "
            f"{type(exc).__name__}: {exc}"
        )
        return

    try:
        before = store.cancel_pending_stripe_order(
            order_id=order_id,
            payment_id=payment_id,
            cancelled_at=cancelled_at,
        )
        store.append_audit_log(
            {
                "AUDIT_ID": f"AUD-{secrets.token_hex(6).upper()}",
                "TIMESTAMP": cancelled_at,
                "USER_ID": current_user.user_id,
                "USER_EMAIL": current_user_email,
                "ACTION": "PENDING_REGISTRATION_CANCELLED",
                "ENTITY_TYPE": "ORDER",
                "ENTITY_ID": order_id,
                "ORDER_ID": order_id,
                "BEFORE_JSON": json.dumps(before, sort_keys=True),
                "AFTER_JSON": json.dumps(
                    {
                        "ORDER_STATUS": "CANCELLED",
                        "PAYMENT_STATUS": "CANCELLED",
                        "REGISTRATION_STATUS": "CANCELLED",
                        "EVENT_ENTRY_STATUS": "CANCELLED",
                        "STRIPE_CHECKOUT_STATUS": stripe_result.get("status", "expired"),
                    },
                    sort_keys=True,
                ),
                "REASON": "Registrant cancelled unpaid registration before payment",
            }
        )
    except Exception as exc:
        st.error(
            "The Stripe Checkout session was expired, but the transaction records "
            "could not be fully cancelled. SAA Admin should review this order. "
            f"{type(exc).__name__}: {exc}"
        )
        return

    _cached_resumable_payment_orders.clear()
    if str((st.session_state.get("pending_checkout", {}) or {}).get("order_id", "")).strip() == order_id:
        st.session_state.pop("pending_checkout", None)
    if str(st.session_state.get("draft_order_id", "") or "").strip() == order_id:
        st.session_state.pop("draft_order_id", None)
    st.session_state.pop("cancel_pending_order_confirm_id", None)
    st.success(f"Pending registration {order_id} was cancelled.")
    st.rerun()


def _render_cancel_pending_confirmation(order_id: str, *, key_prefix: str) -> None:
    confirm_id = str(
        st.session_state.get("cancel_pending_order_confirm_id", "") or ""
    ).strip()
    if confirm_id != str(order_id or "").strip():
        return

    st.warning(
        f"Cancel {order_id}? The unpaid registration will be retained for audit "
        "history but will no longer be resumable or treated as an active entry."
    )
    confirm_col, keep_col = st.columns(2)
    if confirm_col.button(
        "Confirm cancellation",
        type="primary",
        key=f"{key_prefix}_confirm_cancel",
    ):
        _cancel_persisted_payment(order_id)
    if keep_col.button(
        "Keep registration",
        key=f"{key_prefix}_keep_registration",
    ):
        st.session_state.pop("cancel_pending_order_confirm_id", None)
        st.rerun()


def _show_session_pending_checkout() -> None:
    pending = st.session_state.get("pending_checkout", {}) or {}
    if not pending:
        return

    st.success("Your order is pending payment.")
    st.write(f"Amount payable: **SGD {pending.get('amount', '')}**")

    order_id = str(
        pending.get("order_id", "")
        or pending.get("registration_id", "")
        or ""
    ).strip()
    if order_id:
        st.caption(f"Order reference: {order_id}")

    payment_url = str(pending.get("payment_url", "") or "").strip()
    if payment_url:
        st.link_button(
            "Pay by Card or PayNow",
            payment_url,
            type="primary",
        )
    else:
        st.info(
            "Stripe has already accepted or completed this Checkout session. "
            "Please wait for webhook confirmation rather than starting another payment."
        )

    st.warning(
        "The order is not yet confirmed. It will be confirmed only after Stripe "
        "verifies successful payment."
    )

    action_col1, action_col2 = st.columns(2)
    if action_col1.button("Hide this payment prompt for now"):
        st.session_state.pop("pending_checkout", None)
        st.rerun()
    if order_id and action_col2.button(
        "Cancel pending registration",
        key=f"cancel_session_pending_{order_id}",
    ):
        st.session_state["cancel_pending_order_confirm_id"] = order_id
        st.rerun()

    if order_id:
        _render_cancel_pending_confirmation(
            order_id, key_prefix=f"session_pending_{order_id}"
        )

    st.stop()


def _resume_persisted_payment(candidate: dict) -> None:
    order_id = str(candidate.get("order_id", "") or "").strip()
    payment_id = str(candidate.get("payment_id", "") or "").strip()
    if not order_id or not payment_id:
        st.error("This pending order is incomplete and must be reviewed by SAA Admin.")
        return

    stripe_secret_key = str(st.secrets.get("STRIPE_SECRET_KEY", "") or "").strip()
    public_app_url = str(
        st.secrets.get("PUBLIC_APP_URL", "https://saapublicaccess.streamlit.app")
        or ""
    ).strip()
    currency = str(
        candidate.get("currency", "")
        or st.secrets.get("STRIPE_CURRENCY", "sgd")
        or "sgd"
    ).strip().lower()

    if not stripe_secret_key:
        st.error("Stripe is not configured: STRIPE_SECRET_KEY is missing.")
        return
    if currency != "sgd":
        st.error("Stripe PayNow requires STRIPE_CURRENCY to be set to 'sgd'.")
        return

    # Re-read the two authoritative rows immediately before resuming so a stale
    # cached prompt cannot reopen an order that has since been settled/admin-changed.
    store = _transaction_store()
    order_row = store.find_first("ORDERS", "ORDER_ID", order_id) or {}
    payment_row = store.find_first("PAYMENTS", "PAYMENT_ID", payment_id) or {}
    if not order_can_be_resumed(
        order_row,
        payment_row,
        user_id=current_user.user_id,
        organization_id=current_organization.organization_id,
    ):
        _cached_resumable_payment_orders.clear()
        st.info(
            "This order is no longer available for payment. Refresh the page to "
            "see its current status."
        )
        return

    amount = str(
        payment_row.get("AMOUNT", "")
        or order_row.get("TOTAL_AMOUNT", "")
        or candidate.get("amount", "")
    ).strip()
    existing_session_id = str(
        payment_row.get("STRIPE_CHECKOUT_SESSION_ID", "")
        or candidate.get("stripe_session_id", "")
        or ""
    ).strip()
    stripe_status = str(payment_row.get("STRIPE_STATUS", "") or "").strip()

    customer_email = str(
        candidate.get("customer_email", "") or current_user_email
    ).strip()
    athlete_names = list(candidate.get("athlete_names", []) or [])
    athlete_label = (
        athlete_names[0]
        if len(athlete_names) == 1
        else f"{len(athlete_names)} athletes"
    )
    competition_id = str(candidate.get("competition_id", "") or "").strip()
    description = f"{competition_id}: {athlete_label or 'competition registration'}"

    try:
        checkout = get_or_create_registration_checkout(
            secret_key=stripe_secret_key,
            registration_id=order_id,
            amount=amount,
            currency=currency,
            customer_email=customer_email,
            description=description,
            public_app_url=public_app_url,
            existing_session_id=existing_session_id,
            force_new=checkout_force_new(stripe_status),
        )

        attempted_at = dt.datetime.now(dt.timezone.utc).isoformat()
        if checkout.get("payment_status") != "paid":
            store.mark_payment_started(
                order_id=order_id,
                payment_id=payment_id,
                stripe_session_id=checkout.get("session_id", ""),
                attempted_at=attempted_at,
                checkout_url=checkout.get("payment_url", ""),
            )
        elif str(checkout.get("payment_url", "") or "").strip():
            store.update_where(
                "PAYMENTS",
                "PAYMENT_ID",
                payment_id,
                {"STRIPE_CHECKOUT_URL": checkout.get("payment_url", "")},
            )

        # The legacy PendingPayments row is still used by the Stripe webhook
        # to validate the current Checkout Session. When Resume payment creates
        # or reuses a replacement session, keep that row synchronized with the
        # authoritative PAYMENTS row. Otherwise a successful replacement
        # payment can be rejected by the webhook as a session mismatch.
        pending_sheet_url = str(
            st.secrets.get("PENDING_PAYMENT_SHEET_URL", "") or ""
        ).strip()
        pending_worksheet_name = str(
            st.secrets.get("PENDING_PAYMENT_WORKSHEET", "PendingPayments")
            or "PendingPayments"
        ).strip()
        if not pending_sheet_url:
            raise RuntimeError(
                "PENDING_PAYMENT_SHEET_URL is missing; cannot synchronize "
                "the resumed Checkout session."
            )
        google_client = create_google_client(
            dict(st.secrets["gcp_service_account"])
        )
        pending_worksheet = get_pending_worksheet(
            google_client,
            pending_sheet_url,
            pending_worksheet_name,
        )
        retarget_pending_checkout_session(
            worksheet=pending_worksheet,
            registration_id=order_id,
            stripe_session_id=checkout.get("session_id", ""),
        )
    except Exception as exc:
        st.error(f"Unable to resume payment: {type(exc).__name__}: {exc}")
        return

    _cached_resumable_payment_orders.clear()
    st.session_state["draft_order_id"] = order_id
    st.session_state["pending_checkout"] = {
        "order_id": order_id,
        "registration_id": order_id,
        "payment_id": payment_id,
        "session_id": checkout.get("session_id", ""),
        "payment_url": checkout.get("payment_url", ""),
        "amount": amount,
        "currency": currency,
        "athlete_count": candidate.get("athlete_count", 0),
        "event_entry_count": candidate.get("event_entry_count", 0),
    }
    st.rerun()


def _show_persisted_pending_payments() -> None:
    try:
        candidates = _cached_resumable_payment_orders(
            TRANSACTION_SHEET_URL,
            current_user.user_id,
            current_organization.organization_id,
        )
    except Exception as exc:
        st.warning(
            "Could not check for existing pending payments right now: "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if not candidates:
        return

    st.warning(
        "You have an unpaid registration that can be resumed. Resuming uses the "
        "existing order and does not create a duplicate registration."
    )

    candidate_by_id = {row["order_id"]: row for row in candidates}
    order_ids = list(candidate_by_id)
    selected_order_id = st.selectbox(
        "Pending payment",
        order_ids,
        format_func=lambda oid: (
            f"{oid} · {candidate_by_id[oid].get('competition_id', '')} · "
            f"SGD {candidate_by_id[oid].get('amount', '')}"
        ),
        key="resumable_pending_order_id",
    )
    selected = candidate_by_id[selected_order_id]

    names = ", ".join(selected.get("athlete_names", []) or []) or "Registration"
    st.caption(
        f"{names} · {selected.get('event_entry_count', 0)} event entry/entries · "
        f"status {selected.get('display_status', '') or 'pending'}"
    )

    resume_col, cancel_col = st.columns(2)
    if resume_col.button(
        "Resume payment", type="primary", key="resume_persisted_payment"
    ):
        _resume_persisted_payment(selected)
    if cancel_col.button(
        "Cancel pending registration",
        key=f"cancel_persisted_payment_{selected_order_id}",
    ):
        st.session_state["cancel_pending_order_confirm_id"] = selected_order_id
        st.rerun()

    _render_cancel_pending_confirmation(
        selected_order_id, key_prefix=f"persisted_pending_{selected_order_id}"
    )

    st.divider()


_show_session_pending_checkout()
_show_persisted_pending_payments()


apply_pending_text_updates()
init_sheet_session(load_roster_fn=load_roster)

# ---------------- Pilot master/configuration ----------------
try:
    available_competitions = pilot_config.competitions(include_closed=False)
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

if not available_competitions:
    st.info("There are currently no competitions open for registration.")
    st.stop()

_competition_by_id = {
    comp.competition_id: comp for comp in available_competitions
}
_competition_ids = list(_competition_by_id.keys())

# A cart belongs to one competition. While it contains entries the competition
# selector is locked so fee/event rules cannot change underneath the cart.
get_cart()
_cart_locked_competition_id = cart_competition_id()
if (
    cart_has_items()
    and _cart_locked_competition_id
    and _cart_locked_competition_id in _competition_by_id
):
    st.session_state["selected_competition_id"] = _cart_locked_competition_id

selected_competition_id = st.selectbox(
    "Competition",
    options=_competition_ids,
    format_func=lambda cid: _competition_by_id[cid].competition_name,
    key="selected_competition_id",
    disabled=cart_has_items(),
)
if cart_has_items():
    st.caption("Competition is locked while the cart contains entries.")
selected_competition = _competition_by_id[selected_competition_id]
registration_period = pilot_config.registration_period(selected_competition)

try:
    selected_entry_fee = pilot_config.fee_for(
        competition_id=selected_competition_id,
        organization_type=current_organization.organization_type,
        registration_period=registration_period,
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

configured_payment_status = (
    "NO_COST" if selected_entry_fee == Decimal("0") else "REQUIRED"
)

_summary_col1, _summary_col2, _summary_col3, _summary_col4 = st.columns(4)
_summary_col1.metric("Organisation", current_organization.organization_name)
_summary_col2.metric("Account type", current_organization.organization_type.title())
_summary_col3.metric("Registration period", registration_period.title())
_summary_col4.metric("Fee per event", f"S${selected_entry_fee:.2f}")

st.caption(
    f"Team code: {current_organization.team_code} · "
    f"Payment status: {'No cost' if configured_payment_status == 'NO_COST' else 'Required'}"
)

# Preload existing entries from OUTPUT Google Sheet on app start (only if empty)
st.session_state.setdefault("entries", [])
st.session_state.setdefault("full_name", "")
st.session_state.setdefault("name_passport", "")

if not st.session_state.entries:
    _preload_url = (st.session_state.get("output_sheet_url") or "").strip()
    _preload_ws = (st.session_state.get("output_worksheet") or "").strip() or None
    if _preload_url:
        try:
            _df_pre = read_sheet_as_df(_preload_url, worksheet=_preload_ws)
            st.session_state.entries = _sheet_df_to_entries(_df_pre)
        except Exception as e:
            st.session_state["preload_error"] = f"{type(e).__name__}: {repr(e)}"

if st.session_state.get("preload_error"):
    st.warning(f"Could not preload existing entries from output sheet. ({st.session_state['preload_error']})")



if "entries" not in st.session_state:
    st.session_state.entries = []

with st.sidebar:
    st.header("Team / Billing")

    default_team_code = current_organization.team_code
    default_team_name = current_organization.organization_name
    team_name_header = current_organization.organization_name

    st.text_input("Team Name", value=team_name_header, disabled=True)
    st.text_input("Team Code", value=default_team_code, disabled=True)

    st.session_state.setdefault("billing_email", current_user_email)
    billing_name = st.text_input("Billing contact name", value="", key="billing_name")
    billing_email = st.text_input("Billing email", key="billing_email")
    charge_code = st.text_input("Charge code (optional)", value="", key="charge_code")
    po_to_be_sent = st.radio("P/O to be sent", options=["No", "Yes"], index=0, horizontal=True, key="po_to_be_sent")
    if billing_email and not is_valid_email(billing_email):
        st.warning("Billing email looks invalid. Please double-check it.")

    st.divider()
    st.caption(f"Competition: {selected_competition.competition_name}")
    st.caption(f"Registration period: {registration_period.title()}")
    st.caption(f"Entry fee: S${selected_entry_fee:.2f}")

# Defensive: ensure billing fields are bound even if sidebar UI is modified
po_to_be_sent = st.session_state.get("po_to_be_sent", "No")
charge_code = st.session_state.get("charge_code", "")
st.subheader("Athlete Entry Form")

# Athlete fields (no form, so dependent dropdowns update immediately)
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.text_input("Last Name", key="last_name")
with c2:
    st.text_input("First Name", key="first_name")
with c3:
    st.text_input("Other Name (optional)", key="other_name")
with c4:
    gender = st.selectbox("Gender", ["", "Male", "Female"], index=0, key="gender")

# Name as per NRIC/Passport is a separate required field.
# It is intentionally NOT auto-filled from First Name / Last Name / Full Name.
passport_name = st.text_input("Name as per NRIC/Passport", key="name_passport")
passport_ok = bool((passport_name or "").strip())
if not passport_ok:
    st.warning("Name as per NRIC/Passport is required.")

# Live validation: gender (mandatory)
gender_ok = gender in ('Male','Female')
if not gender_ok:
    st.warning("Gender is required (select Male or Female).")


last_name = st.session_state.get("last_name", "")
first_name = st.session_state.get("first_name", "")
other_name = st.session_state.get("other_name", "")

# If user edits any name fields after selecting from roster, clear roster-derived FULL_NAME and UNIQUE_ID
current_name_sig = "|".join([
    (st.session_state.get("first_name", "") or "").strip(),
    (st.session_state.get("other_name", "") or "").strip(),
    (st.session_state.get("last_name", "") or "").strip(),
])
prev_sig = (st.session_state.get("full_name_signature", "") or "").strip()
if prev_sig and current_name_sig != prev_sig:
    # User typed a new name; clear roster-derived fields so they don't persist
    st.session_state["full_name__pending"] = ""
    st.session_state["unique_id_override__pending"] = ""
    st.session_state["db_name_override__pending"] = ""
    st.session_state["full_name_signature__pending"] = ""
    st.rerun()


# Roster match selector (Google Sheet) — selecting a row fills fields (no splitting)
search_text = (" ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()])).strip()

_roster_enabled = bool(st.session_state.get("use_roster"))
_roster_url = (st.session_state.get("roster_sheet_url") or "").strip()

# Small inline hints so you can see why the dropdown may not appear
if not _roster_enabled:
    st.caption("Roster search is OFF (check configuration).")
elif not _roster_url:
    st.caption("Roster sheet URL is empty (set ROSTER_SHEET_URL in secrets).")
elif len(search_text) < 2:
    st.caption("Type at least 2 characters in First/Other/Last name to search the roster.")

matches = []
roster_rows = []
if _roster_enabled and _roster_url and len(search_text) >= 2:
    try:
        roster_rows = st.session_state.get("roster_cache_rows")
        # If cache exists but is empty, try one reload per session (handles first-run load glitches)
        if isinstance(roster_rows, list) and (len(roster_rows) == 0) and (not st.session_state.get("roster_cache_reloaded_once")):
            st.session_state["roster_cache_reloaded_once"] = True
            roster_rows = load_roster(
                st.session_state.get("roster_sheet_url", ""),
                worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
            )
            st.session_state["roster_cache_rows"] = roster_rows
        if not isinstance(roster_rows, list):
            roster_rows = load_roster(
                st.session_state.get("roster_sheet_url", ""),
                worksheet=((st.session_state.get("roster_worksheet") or "").strip() or None),
            )
            st.session_state["roster_cache_rows"] = roster_rows
    except Exception as e:
        st.error(f"Roster load error: {type(e).__name__}: {repr(e)}")
        roster_rows = []

    st.caption(f"Roster loaded: {len(roster_rows)} rows")
    q = search_text.casefold()
    for r in roster_rows:
        full_name = str(r.get("FULL_NAME", "") or "")
        fn = str(r.get("FIRST_NAME", "") or "")
        ln = str(r.get("LAST_NAME", "") or "")
        on = str(r.get("OTHER_NAME", "") or "")
        team = str(r.get("TEAM_NAME", "") or "")
        uid = str(r.get("UNIQUE_ID", "") or "")
        nric = str(r.get("NRIC", "") or "")
        # Match on tokens across ALL name fields (FIRST/LAST/OTHER/FULL), plus team/uid/nric(last4)
        full_name = str(r.get("FULL_NAME", "") or "")
        name_hay = " ".join([full_name, fn, on, ln]).casefold()
        tokens = [t.casefold() for t in search_text.split() if t.strip()]
        extra_hay = " ".join([team, uid, last4_from_nric(nric)]).casefold()
        # Scored OR-matching: show suggestions even if only part of the name is typed
        score = 0
        if tokens:
            score += sum(1 for t in tokens if t in name_hay)
        if q and q in name_hay:
            score += 2  # boost full-query name hits
        if q and q in extra_hay:
            score += 1
        if score > 0:
            matches.append((score, r))

    if matches:
        matches = [r for _, r in sorted(matches, key=lambda x: x[0], reverse=True)]
    st.caption(f"Matches found: {len(matches)}")

    if roster_rows and not matches:
        st.info(f"No roster matches for: '{search_text}'. You can refine the search (try first name, last name, team, UID, or NRIC last-4).")

    # Optional browse mode (helps confirm data is loading)
    browse_mode = st.toggle("Browse roster (show first 25)", value=False, key="browse_roster_mode")
    if browse_mode and roster_rows:
        matches = roster_rows[:25]

    if matches:
        labels = []
        for r in matches[:25]:
            fn = str(r.get("FIRST_NAME", "") or "").strip()
            fn_raw = str(r.get("FIRST_NAME", "") or "").strip()
            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            nric = str(r.get("NRIC", "") or "").strip()
            dob_val = parse_dob(r.get("DOB"))
            # Privacy: only show birth year in roster match/browse labels, not full DOB.
            if hasattr(dob_val, "strftime") and dob_val:
                dob_str = dob_val.strftime("%Y")
            else:
                _dob_raw = str(r.get("DOB", "") or "").strip()
                _year_match = re.search(r"(?:19|20)\d{2}", _dob_raw)
                dob_str = _year_match.group(0) if _year_match else ""
            gen = str(r.get("GENDER", "") or "").strip()
            nat = str(r.get("NATIONALITY", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            tcode = str(r.get("TEAM_CODE", "") or "").strip()
            team = str(r.get("TEAM_NAME", "") or "").strip()

            # Privacy: roster dropdown labels show first name only and redact last name.
            # Underlying roster row still contains full details for autofill after selection.
            first_for_label = fn or str(r.get("FIRST_NAME", "") or "").strip()
            last_for_label = ln or str(r.get("LAST_NAME", "") or "").strip()
            label_parts = []
            if first_for_label:
                label_parts.append(first_for_label)
            if last_for_label:
                label_parts.append("*")
            label = " ".join(label_parts).strip() or "(unnamed roster entry)"
            parts = []
            n4 = last4_from_nric(nric)
            if n4:
                parts.append(f"NRIC(last4): {n4}")
            if dob_str:
                parts.append(f"{dob_str}")
            if gen:
                parts.append(f"{gen}")
            if nat:
                parts.append(f"{nat}")
            team_piece = " ".join([p for p in [tcode, team] if p]).strip()
            if team_piece:
                parts.append(f"{team_piece}")
            if parts:
                label = label + " | " + " | ".join(parts)
            labels.append(label)

        sel_key = "athlete_roster_match"
        options = ["(keep typed)"] + list(range(len(labels)))
        chosen = st.selectbox(
            "Select From List of Matches :",
            options=options,
            key=sel_key,
            format_func=lambda x: "(keep typed)" if x == "(keep typed)" else labels[int(x)],
        )

        if chosen != "(keep typed)":
            idx = int(chosen)
            r = matches[idx]

            full_name_sel = str(r.get("FULL_NAME", "") or "").strip()
            if not full_name_sel:
                fn_tmp = str(r.get("FIRST_NAME", "") or "").strip()
                on_tmp = str(r.get("OTHER_NAME", "") or "").strip()
                ln_tmp = str(r.get("LAST_NAME", "") or "").strip()
                full_name_sel = " ".join([p for p in [fn_tmp, on_tmp, ln_tmp] if p]).strip()

            fn = str(r.get("FIRST_NAME", "") or "").strip()
            fn_raw = str(r.get("FIRST_NAME", "") or "").strip()
            ln = str(r.get("LAST_NAME", "") or "").strip()
            on = str(r.get("OTHER_NAME", "") or "").strip()
            nric = str(r.get("NRIC", "") or "").strip()
            dob = parse_dob(r.get("DOB"))
            gender_raw = str(r.get("GENDER", "") or "").strip().upper()
            nat_raw = str(r.get("NATIONALITY", "") or "").strip()
            sgpr_raw = str(r.get("SINGAPORE_PR", "") or r.get("SG_PR", "") or r.get("PR_STATUS", "") or "").strip()
            uid = str(r.get("UNIQUE_ID", "") or "").strip()
            tname = str(r.get("TEAM_NAME", "") or "").strip()
            tcode_raw = str(r.get("TEAM_CODE", "") or "").strip()

            # Populate name fields
            st.session_state["first_name__pending"] = fn_raw or on
            st.session_state["last_name__pending"] = ln
            st.session_state["other_name__pending"] = on
            st.session_state["full_name__pending"] = full_name_sel
            roster_name_passport = str(
                r.get("NAME_PASSPORT", "")
                or r.get("NAME_AS_PER_NRIC_PASSPORT", "")
                or r.get("NAME AS PER NRIC/PASSPORT", "")
                or ""
            ).strip()
            if roster_name_passport:
                st.session_state["name_passport__pending"] = roster_name_passport
            st.session_state["full_name_signature__pending"] = "|".join([
                (st.session_state.get("first_name__pending", "") or "").strip(),
                (st.session_state.get("other_name__pending", "") or "").strip(),
                (st.session_state.get("last_name__pending", "") or "").strip(),
            ])

            # Populate other fields
            st.session_state["ic_last4__pending"] = last4_from_nric(nric)
            st.session_state["birth_date__pending"] = dob
            # Gender from roster (may be blank; still mandatory to submit)
            if gender_raw in ("M","F","MALE","FEMALE"):
                st.session_state["gender__pending"] = ("Male" if gender_raw.startswith("M") else "Female")
            else:
                st.session_state["gender__pending"] = ""

            # Nationality: if not in list, store as override so it still appears in the dropdown
            nat_pick = _match_option_case_insensitive(nat_raw, (COUNTRIES or []))
            if nat_pick:
                st.session_state["nationality__pending"] = nat_pick
                st.session_state["nationality_override__pending"] = ""
            else:
                st.session_state["nationality__pending"] = nat_raw
                st.session_state["nationality_override__pending"] = nat_raw

            _nat_cf = nat_raw.casefold()
            _sgpr_cf = sgpr_raw.casefold()
            roster_is_sg_pr = (
                _sgpr_cf in ("yes", "y", "true", "1", "pr", "singapore pr", "sg pr")
                or _nat_cf in ("singapore pr", "sg pr")
            )
            st.session_state["singapore_pr__pending"] = bool(roster_is_sg_pr)
            if roster_is_sg_pr and not nat_pick:
                # Keep nationality as an IOC/WA code while using the checkbox for PR status.
                st.session_state["nationality__pending"] = "SGP"
                st.session_state["nationality_override__pending"] = ""

            # Unique ID override from roster
            st.session_state["unique_id_override__pending"] = uid
            # Optional roster fields, if available
            roster_email = str(r.get("EMAIL", "") or r.get("Email", "") or "").strip()
            roster_contact = str(
                r.get("CONTACT_NUMBER", "")
                or r.get("CONTACT", "")
                or r.get("MOBILE", "")
                or r.get("PHONE", "")
                or ""
            ).strip()
            if roster_email:
                st.session_state["email__pending"] = roster_email
            if roster_contact:
                st.session_state["contact_number__pending"] = roster_contact


            # Team fields:
            # The form now uses Team Name as the selected widget and shows Team Code automatically.
            # Therefore we must set BOTH the code-related state and the team_name_selected widget key.
            tcode_pick = _match_option_case_insensitive(tcode_raw, TEAM_CODES)
            resolved_team_code = tcode_pick or tcode_raw
            resolved_team_name = tname or (get_team_name(resolved_team_code) if resolved_team_code else "")

            if tcode_pick:
                st.session_state["team_code__pending"] = tcode_pick
                st.session_state["team_code_override__pending"] = ""
            else:
                st.session_state["team_code__pending"] = tcode_raw
                st.session_state["team_code_override__pending"] = tcode_raw

            if resolved_team_name:
                st.session_state["team_name_selected__pending"] = resolved_team_name
                st.session_state["team_name_override__pending"] = resolved_team_name
            else:
                st.session_state["team_name_selected__pending"] = ""
                st.session_state["team_name_override__pending"] = ""

            st.session_state["athlete_roster_match__pending"] = "(keep typed)"
            st.rerun()


# Combined name (display)
typed_full_name = " ".join([p for p in [first_name, other_name, last_name] if (p or "").strip()]).strip()
db_name_override = (st.session_state.get("db_name_override", "") or "").strip()

# Full Name (auto) — editable
full_name_display = (st.session_state.get("full_name", "") or "").strip()
if (not full_name_display) and typed_full_name:
    # Pre-fill from typed First/Other/Last (user can edit)
    st.session_state["full_name"] = typed_full_name
    full_name_display = typed_full_name
st.text_input("Full Name (auto)", key="full_name")

# Live validation: name presence
selected_from_roster = bool((st.session_state.get("unique_id_override", "") or "").strip() or (db_name_override or "").strip())
first_last_ok = selected_from_roster or (bool((first_name or "").strip()) and bool((last_name or "").strip()))
name_ok = passport_ok and first_last_ok
if not first_last_ok:
    st.warning("First Name and Last Name are required unless you selected the athlete from the roster.")


c4, c5, c6 = st.columns(3)
with c4:
    # Birth Date input (allow rendering even if a preloaded value is later than today)
    _birth_cur = st.session_state.get("birth_date")
    _birth_max = dt.date.today()
    if isinstance(_birth_cur, dt.date) and _birth_cur > _birth_max:
        _birth_max = _birth_cur
    birth_date = st.date_input(
        "Birth Date",
        value=_birth_cur if isinstance(_birth_cur, dt.date) else None,
        min_value=dt.date(1900, 1, 1),
        max_value=_birth_max,
        key="birth_date",
        format="DD-MM-YYYY",
    )
    # Live validation: birth date
    birth_ok = (st.session_state.get("birth_date") is not None) and (st.session_state.get("birth_date") <= dt.date.today())
    if st.session_state.get("birth_date") and st.session_state.get("birth_date") > dt.date.today():
        st.warning("Birth Date cannot be in the future.")
    elif not birth_ok:
        st.warning("Birth Date is required.")

with c6:
    nationality_options = [""] + (COUNTRIES or [])
    _nat_extra = (st.session_state.get("nationality_override", "") or "").strip()
    if _nat_extra and _nat_extra not in nationality_options:
        nationality_options = ["", _nat_extra] + [x for x in nationality_options if x != ""]
    nationality = st.selectbox("Nationality", nationality_options, index=0, key="nationality")
    nationality_ok = bool(str(nationality or "").strip())
    if not nationality_ok:
        st.warning("Nationality is required.")
    is_singapore = (str(nationality or '').strip().upper() in ('SGP','SIN','SG','SINGAPORE'))
    # Singapore PR status (separate from nationality code)
    singapore_pr = st.checkbox('Singapore PR?', key='singapore_pr')



# Unique ID (display) — placed under Birth Date / IC row
unique_id_override = (st.session_state.get("unique_id_override", "") or "").strip()
_ic_for_uid = normalize_ic_last4(st.session_state.get("ic_last4", "") or "")
unique_id = unique_id_override or (compute_unique_id(first_name, _ic_for_uid, birth_date) if birth_date else "")
uid_from_roster = unique_id_override
if uid_from_roster:
    st.text_input("Unique ID (from roster)", value=uid_from_roster, disabled=True)
else:
    st.text_input("Unique ID (auto)", value=(unique_id or ""), disabled=True)


with c5:
    ic_last4 = st.text_input("IC Number (last 4)", key="ic_last4")
    # Live validation: IC last-4 (3 digits + 1 letter)
    ic_last4_norm = normalize_ic_last4(ic_last4)  # ALWAYS define
    # IC last-4 is required if Singapore PR is ticked, or if Singapore athlete has no UNIQUE_ID
    unique_id_present_for_ic = bool((st.session_state.get("unique_id_override", "") or "").strip() or (st.session_state.get("unique_id", "") or "").strip())
    ic_required = bool(singapore_pr) or (bool(is_singapore) and (not unique_id_present_for_ic))
    ic_ok = True
    if ic_required and not ic_last4_norm:
        ic_ok = False
        st.warning("IC format: 3 digits + 1 letter (e.g., 123A) — required when Singapore PR is ticked, or when a Singapore athlete has no UNIQUE_ID.")
    elif (not ic_last4_norm):
        # Not required and not provided
        ic_ok = True
    elif len(ic_last4_norm) < 4:
        ic_ok = False
        st.warning("IC last 4 is incomplete (e.g., 123A).")
    else:
        ic_ok = is_valid_ic_last4(ic_last4_norm)
        if not ic_ok:
            st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")

c7, c8 = st.columns(2)
contact_number = c7.text_input("Contact Number", key="contact_number")

# Live validation: contact number
contact_ok = bool((contact_number or '').strip())
if not contact_ok:
    st.warning("Contact Number is required.")

email = c8.text_input("Email", key="email")

# Live validation: email
email_norm = normalize_email(email)
email_ok = True
if email_norm:
    email_ok = is_valid_email(email_norm)

email_present = bool(email_norm)
if not email_present:
    st.warning("Email is required.")
    if not email_ok:
        st.error("Please enter a valid email address (e.g., name@example.com).")


c9, c10 = st.columns(2)

team_name_row = current_organization.organization_name
team_code = current_organization.team_code
st.session_state["team_code"] = team_code
c9.text_input("Team Name", value=team_name_row, disabled=True)
c10.text_input("Team Code", value=team_code, disabled=True)

# Divisions and events are driven by the competition configuration worksheets.
c11, c12 = st.columns(2)
try:
    _division_rows = pilot_config.division_rows(
        competition_id=selected_competition_id,
        gender=gender,
        birth_date=birth_date if birth_ok else None,
        competition_start_at=selected_competition.competition_start_at,
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

_active_division_keys = [row["code"] for row in _division_rows]
_division_labels = {
    row["code"]: row["label"] for row in _division_rows
}

if _active_division_keys:
    if st.session_state.get("event_division") not in _active_division_keys:
        st.session_state["event_division"] = _active_division_keys[0]

event_division = c11.selectbox(
    "Event Division",
    options=_active_division_keys,
    format_func=lambda code: (
        f"{code} - {_division_labels.get(code, code)}"
        if _division_labels.get(code, code) != code
        else code
    ),
    key="event_division",
    disabled=(not _active_division_keys),
)

try:
    event_opts_raw = (
        pilot_config.event_options(
            competition_id=selected_competition_id,
            gender=gender,
            division_code=event_division if _active_division_keys else "",
        )
        if gender_ok and _active_division_keys
        else []
    )
except PilotConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

event_opts = sorted(event_opts_raw, key=lambda _x: _event_sort_key(_x[0]))
event_names = [name for name, _code in event_opts]

prev_selected = st.session_state.get("events_selected", [])
if not isinstance(prev_selected, list):
    prev_selected = []
prev_selected_valid = [e for e in prev_selected if e in event_names]
if prev_selected_valid != prev_selected:
    st.session_state["events_selected"] = prev_selected_valid

selected_events = c12.multiselect(
    "Select event(s)",
    options=event_names,
    default=prev_selected_valid,
    key="events_selected",
    disabled=(not event_names),
)

# Live validation: DOB first determines age-eligible divisions; gender + division
# then determine the configured event list.
event_ok = bool(event_opts) and len(selected_events) > 0

if not gender_ok:
    st.info("Select Gender to load the available events.")
elif birth_ok and not _active_division_keys:
    athlete_age = pilot_config.age_on_date(
        birth_date,
        selected_competition.competition_start_at,
    )
    competition_date = (
        selected_competition.competition_start_at.date()
        if selected_competition.competition_start_at is not None
        else None
    )
    age_text = f"age {athlete_age}" if athlete_age is not None else "this age"
    date_text = (
        f" on {competition_date.strftime('%d-%m-%Y')}"
        if competition_date is not None
        else ""
    )
    st.warning(
        "No eligible divisions with configured events are available for "
        f"{age_text}{date_text}."
    )
elif _active_division_keys and not event_names:
    st.warning(
        "No active events are configured for "
        f"{gender} / {event_division} in {selected_competition.competition_name}."
    )
elif not selected_events:
    st.warning("Please select at least one event for the selected division.")


def _season_best_state_key(event_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", str(event_name or "")).strip("_")
    return f"season_best_event__{safe}"


season_best_by_event = {}
if selected_events:
    st.markdown("**Season Best by event**")
    st.caption("Enter one Season Best for each selected event.")
    _sb_columns = st.columns(min(3, max(1, len(selected_events))))
    for _sb_index, _sb_event in enumerate(selected_events):
        with _sb_columns[_sb_index % len(_sb_columns)]:
            _sb_value = st.text_input(
                f"{_sb_event} Season Best",
                key=_season_best_state_key(_sb_event),
            )
        season_best_by_event[_sb_event] = str(_sb_value or "").strip()

season_best_ok = bool(selected_events) and all(
    season_best_by_event.get(event_name, "")
    for event_name in selected_events
)
if selected_events and not season_best_ok:
    st.warning("Season Best is required for every selected event.")
emergency_contact_name = st.text_input("Emergency Contact Name", key="emergency_contact_name")
emergency_contact_number = st.text_input("Emergency Contact Number", key="emergency_contact_number")
coach_full_name = st.text_input("Coach Full Name", key="coach_full_name")
parq = st.selectbox("PAR-Q completed?", ["Y", "N"], key="parq")

ic_last4_norm = normalize_ic_last4(ic_last4)
email_norm = normalize_email(email)

# The waiver is accepted once for the whole order at cart review, rather than
# once for each athlete added to the same order.
ready_to_add = (
    bool(email_present)
    and bool(email_ok)
    and bool(ic_ok)
    and bool(birth_ok)
    and bool(contact_ok)
    and bool(name_ok)
    and bool(gender_ok)
    and bool(nationality_ok)
    and bool(event_ok)
    and bool(season_best_ok)
)



# Pending Checkout recovery is rendered immediately after authentication so it
# remains available even if later competition/master-data reads fail.


def _new_id(prefix: str) -> str:
    return (
        prefix
        + "-"
        + secrets.token_urlsafe(9)
        .replace("-", "")
        .replace("_", "")
        .upper()
    )


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _draft_order_id() -> str:
    order_id = str(st.session_state.get("draft_order_id", "") or "").strip()
    if not order_id:
        order_id = _new_id("ORD")
        st.session_state["draft_order_id"] = order_id
    return order_id


def _existing_entries_for_integrity(
    store: TransactionSheetStore,
    *,
    ignore_order_id: str = "",
) -> list[dict[str, str]]:
    rows = store.list_rows("EVENT_ENTRIES")
    ignored = str(ignore_order_id or "").strip()
    if not ignored:
        return rows
    return [
        row
        for row in rows
        if str(row.get("ORDER_ID", "") or "").strip() != ignored
    ]


def _candidate_summary(candidate: dict) -> dict:
    return {
        "REGISTRATION_ID": candidate.get("registration_id", ""),
        "COMPETITION_ID": candidate.get("competition_id", ""),
        "ORGANIZATION_ID": candidate.get("organization_id", ""),
        "TEAM_CODE": candidate.get("team_code", ""),
        "ATHLETE_ID": candidate.get("athlete_id", ""),
        "ATHLETE_NAME": candidate.get("athlete_name", ""),
        "DOB": candidate.get("dob", ""),
        "EVENTS": candidate.get("events", []),
    }


def _audit_integrity_conflicts(
    *,
    store: TransactionSheetStore,
    candidate: dict,
    result,
    stage: str,
) -> None:
    action_by_code = {
        "DUPLICATE_EVENT": "ATHLETE_DUPLICATE_EVENT_BLOCKED",
        "TEAM_CONFLICT": "ATHLETE_TEAM_CONFLICT_BLOCKED",
        "IDENTITY_COLLISION": "ATHLETE_IDENTITY_COLLISION_BLOCKED",
    }
    now_iso = _iso_now()
    for conflict in result.conflicts:
        before = {
            "ENTRY_ID": conflict.existing_entry_id,
            "ORDER_ID": conflict.existing_order_id,
            "REGISTRATION_ID": conflict.existing_registration_id,
            "ORGANIZATION_ID": conflict.existing_organization_id,
            "TEAM_CODE": conflict.existing_team_code,
            "EVENT_NAME": conflict.existing_event_name,
            "EVENT_CODE": conflict.existing_event_code,
            "ATHLETE_ID": conflict.existing_athlete_id,
            "ATHLETE_NAME": conflict.existing_athlete_name,
            "DOB": conflict.existing_dob,
        }
        store.append_audit_log(
            {
                "AUDIT_ID": _new_id("AUD"),
                "TIMESTAMP": now_iso,
                "USER_ID": current_user.user_id,
                "USER_EMAIL": current_user_email,
                "ACTION": action_by_code.get(
                    conflict.code, "ATHLETE_INTEGRITY_BLOCKED"
                ),
                "ENTITY_TYPE": "REGISTRATION_INTEGRITY",
                "ENTITY_ID": (
                    candidate.get("registration_id", "")
                    or candidate.get("athlete_id", "")
                    or "UNRESOLVED_ATHLETE"
                ),
                "ORDER_ID": conflict.existing_order_id,
                "BEFORE_JSON": json.dumps(before, sort_keys=True),
                "AFTER_JSON": json.dumps(
                    _candidate_summary(candidate), sort_keys=True
                ),
                "REASON": f"Phase 5A {stage}: {conflict.code}",
            }
        )


def _render_integrity_conflicts(result) -> None:
    st.error(
        "This athlete cannot be added/submitted until the registration "
        "integrity issue below is resolved."
    )
    for conflict in result.conflicts:
        reference_bits = []
        if conflict.existing_entry_id:
            reference_bits.append(f"entry {conflict.existing_entry_id}")
        if conflict.existing_order_id:
            reference_bits.append(f"order {conflict.existing_order_id}")
        reference = (
            " Existing record: " + ", ".join(reference_bits) + "."
            if reference_bits
            else ""
        )
        st.warning(f"{conflict.message}{reference}")


def _check_cart_item_against_transactions(
    cart_item: dict,
    *,
    store: TransactionSheetStore | None = None,
    existing_entries: list[dict[str, str]] | None = None,
    ignore_order_id: str = "",
    stage: str = "cart-add check",
):
    integrity_store = store or _transaction_store()
    rows = (
        existing_entries
        if existing_entries is not None
        else _existing_entries_for_integrity(
            integrity_store, ignore_order_id=ignore_order_id
        )
    )
    candidate = candidate_from_cart_item(cart_item)
    result = check_candidate_against_existing(candidate, rows)
    if result.blocked:
        try:
            _audit_integrity_conflicts(
                store=integrity_store,
                candidate=candidate,
                result=result,
                stage=stage,
            )
        except Exception as audit_exc:
            st.warning(
                "The registration was blocked correctly, but its audit record "
                f"could not be written: {type(audit_exc).__name__}: {audit_exc}"
            )
    return result


def _validate_cart_against_transactions(
    cart_items: list[dict],
    *,
    ignore_order_id: str = "",
) -> bool:
    try:
        store = _transaction_store()
        existing_entries = _existing_entries_for_integrity(
            store, ignore_order_id=ignore_order_id
        )
    except Exception as exc:
        st.error(
            "Unable to verify existing competition registrations. The order "
            "has not been submitted. Please retry. "
            f"({type(exc).__name__}: {exc})"
        )
        return False

    blocked_results = []
    for item in cart_items:
        result = _check_cart_item_against_transactions(
            item,
            store=store,
            existing_entries=existing_entries,
            ignore_order_id=ignore_order_id,
            stage="pre-submit recheck",
        )
        if result.blocked:
            blocked_results.append(result)

    if blocked_results:
        st.error(
            "The cart is no longer valid because an athlete registration "
            "conflict now exists. No order or payment was created."
        )
        for result in blocked_results:
            _render_integrity_conflicts(result)
        return False
    return True


def _competition_rule_tables(*, fresh: bool = False):
    """Load the three master tables required for Phase 5C validation.

    For a final pre-submit check, explicitly clear both configuration cache
    layers before reading the worksheets again.  Deliberately call
    ``PilotConfigRepository.table()`` with its long-standing one-argument
    interface so a Streamlit hot reload cannot leave the page code coupled to
    a newer repository method signature.
    """
    if fresh:
        clear_config_cache()
        # ``read_sheet_as_df`` is a Streamlit cached function in both the
        # pre-5C and 5C readers. Clearing it guarantees the repository's next
        # reads reach Google Sheets rather than the 30-second data cache.
        clear_reader_cache = getattr(read_sheet_as_df, "clear", None)
        if callable(clear_reader_cache):
            clear_reader_cache()

    return (
        pilot_config.table("COMPETITIONS"),
        pilot_config.table("DIVISIONS"),
        pilot_config.table("COMPETITION_EVENTS"),
    )


def _audit_competition_rule_issues(
    *,
    result,
    stage: str,
    order_id: str = "",
) -> None:
    if not result.blocked:
        return

    store = _transaction_store()
    now_iso = _iso_now()
    for issue in result.issues:
        if issue.code == "CONFIG_ERROR":
            action = "COMPETITION_RULE_CONFIG_ERROR"
        elif stage == "pre-submit recheck":
            action = "COMPETITION_RULES_CHANGED_BLOCKED"
        elif issue.code == "DIVISION_INELIGIBLE":
            action = "ATHLETE_DIVISION_INELIGIBLE_BLOCKED"
        elif issue.code == "EVENT_INELIGIBLE":
            action = "ATHLETE_EVENT_INELIGIBLE_BLOCKED"
        else:
            action = "COMPETITION_UNAVAILABLE_BLOCKED"

        store.append_audit_log(
            {
                "AUDIT_ID": _new_id("AUD"),
                "TIMESTAMP": now_iso,
                "USER_ID": current_user.user_id,
                "USER_EMAIL": current_user_email,
                "ACTION": action,
                "ENTITY_TYPE": "COMPETITION_RULE",
                "ENTITY_ID": (
                    issue.event_code
                    or issue.event_name
                    or issue.division_code
                    or issue.athlete_name
                    or issue.competition_id
                    or "UNRESOLVED_RULE"
                ),
                "ORDER_ID": order_id,
                "BEFORE_JSON": "",
                "AFTER_JSON": json.dumps(issue.as_dict(), sort_keys=True),
                "REASON": f"Phase 5C {stage}: {issue.code}",
            }
        )


def _render_competition_rule_issues(result, *, final_recheck: bool) -> None:
    if final_recheck:
        st.error(
            "The cart is no longer valid because the current competition "
            "eligibility rules do not permit one or more entries. "
            "No order or payment was created."
        )
    else:
        st.error(
            "This athlete cannot be added until the competition/division "
            "eligibility issue below is resolved."
        )

    for issue in result.issues:
        st.warning(issue.message)


def _validate_cart_against_competition_rules(
    cart_items: list[dict],
    *,
    fresh: bool,
    stage: str,
    order_id: str = "",
) -> bool:
    try:
        competition_rows, division_rows, competition_event_rows = (
            _competition_rule_tables(fresh=fresh)
        )
        result = validate_cart_competition_rules(
            cart_items,
            competition_id=selected_competition_id,
            competition_rows=competition_rows,
            division_rows=division_rows,
            competition_event_rows=competition_event_rows,
        )
    except Exception as exc:
        st.error(
            "Unable to verify the current competition eligibility rules. "
            "The registration has not been submitted. Please retry. "
            f"({type(exc).__name__}: {exc})"
        )
        return False

    if not result.blocked:
        return True

    try:
        _audit_competition_rule_issues(
            result=result,
            stage=stage,
            order_id=order_id,
        )
    except Exception as audit_exc:
        st.warning(
            "The registration was blocked correctly, but its Phase 5C audit "
            "record could not be written: "
            f"{type(audit_exc).__name__}: {audit_exc}"
        )

    _render_competition_rule_issues(
        result, final_recheck=(stage == "pre-submit recheck")
    )
    return False


def _same_cart_athlete(candidate_a: dict, candidate_b: dict) -> bool:
    id_a = str(candidate_a.get("athlete_id", "") or "").strip().casefold()
    id_b = str(candidate_b.get("athlete_id", "") or "").strip().casefold()
    if id_a and id_b:
        return id_a == id_b

    name_a = normalise_person_name(candidate_a.get("athlete_name", ""))
    name_b = normalise_person_name(candidate_b.get("athlete_name", ""))
    dob_a = str(candidate_a.get("dob", "") or "").strip()[:10]
    dob_b = str(candidate_b.get("dob", "") or "").strip()[:10]
    return bool(name_a and name_b and dob_a and dob_b and name_a == name_b and dob_a == dob_b)


def _system_int(key: str, default: int) -> int:
    raw = pilot_config.system_value(key, str(default))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _build_transaction_bundle(
    *,
    order_id: str,
    payment_id: str,
    payment_type: str,
    order_status: str,
    registration_status: str,
    event_status: str,
    payment_status: str,
    technical_payment_status: str,
    total_amount: Decimal,
    cart: list[dict],
    waiver_signer_name: str,
    waiver_version: str,
) -> dict:
    now_iso = _iso_now()

    expiry = ""
    if payment_type == "STRIPE":
        expiry_hours = _system_int("PENDING_PAYMENT_EXPIRY_HOURS", 72)
        expiry = (
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(hours=expiry_hours)
        ).isoformat()

    order = {
        "ORDER_ID": order_id,
        "COMPETITION_ID": selected_competition_id,
        "USER_ID": current_user.user_id,
        "ORGANIZATION_ID": current_organization.organization_id,
        "ENTRY_COUNT": cart_total_event_entries(),
        "ENTRY_SUBTOTAL": f"{total_amount:.2f}",
        "PROCESSING_FEE": "",
        "TOTAL_AMOUNT": f"{total_amount:.2f}",
        "PAYMENT_TYPE": payment_type,
        "STATUS": order_status,
        "CREATED_AT": now_iso,
        "EXPIRES_AT": expiry,
        "UPDATED_AT": now_iso,
    }

    registrations = []
    event_entries = []

    for item in cart:
        rows = item.get("entry_rows", []) or []
        first_row = rows[0] if rows else {}

        registrations.append(
            {
                "REGISTRATION_ID": item.get("registration_id", ""),
                "ORDER_ID": order_id,
                "COMPETITION_ID": selected_competition_id,
                "ATHLETE_ID": first_row.get("unique_id", ""),
                "ORGANIZATION_ID": current_organization.organization_id,
                "SUBMITTED_BY_USER_ID": current_user.user_id,
                "DIVISION": item.get("division", ""),
                "STATUS": registration_status,
                "CREATED_AT": now_iso,
                "UPDATED_AT": now_iso,
                "IS_DELETED": False,
                "ATHLETE_NAME": item.get("athlete_name", ""),
                "DOB": first_row.get("birth_date", ""),
                "GENDER": first_row.get("gender", ""),
                "NATIONALITY": first_row.get("nationality", ""),
                "TEAM_CODE": current_organization.team_code,
                "TEAM_NAME": current_organization.organization_name,
                "EMAIL": item.get("athlete_email", ""),
                "CONTACT_NUMBER": first_row.get("contact_number", ""),
                "FIRST_NAME": first_row.get("first_name", ""),
                "OTHER_NAME": first_row.get("other_name", ""),
                "LAST_NAME": first_row.get("last_name", ""),
            }
        )

        for row in rows:
            event_entries.append(
                {
                    "ENTRY_ID": row.get("entry_id", ""),
                    "REGISTRATION_ID": item.get("registration_id", ""),
                    "ORDER_ID": order_id,
                    "PAYMENT_ID": payment_id,
                    "COMPETITION_ID": selected_competition_id,
                    "ATHLETE_ID": row.get("unique_id", ""),
                    "ORGANIZATION_ID": current_organization.organization_id,
                    "EVENT_NAME": row.get("event", ""),
                    "EVENT_CODE": row.get("event_code", ""),
                    "DIVISION": row.get("event_division", ""),
                    "SEASON_BEST": row.get("season_best", ""),
                    "ENTRY_FEE": row.get("entry_fee", ""),
                    "REGISTRATION_PERIOD": row.get(
                        "registration_period", registration_period
                    ),
                    "PAYMENT_STATUS": payment_status,
                    "PAYMENT_STATUS_CHANGED_AT": now_iso,
                    "STATUS": event_status,
                    "CREATED_AT": now_iso,
                    "UPDATED_AT": now_iso,
                    "IS_DELETED": False,
                    "ATHLETE_NAME": row.get("full_name", ""),
                    "DOB": row.get("birth_date", ""),
                    "GENDER": row.get("gender", ""),
                    "NATIONALITY": row.get("nationality", ""),
                    "TEAM_CODE": current_organization.team_code,
                    "TEAM_NAME": current_organization.organization_name,
                    "EMAIL": row.get("email", ""),
                    "CONTACT_NUMBER": row.get("contact_number", ""),
                    "FIRST_NAME": row.get("first_name", ""),
                    "OTHER_NAME": row.get("other_name", ""),
                    "LAST_NAME": row.get("last_name", ""),
                }
            )

    waiver = build_waiver_record(
        order_id=order_id,
        competition_id=selected_competition_id,
        organization_id=current_organization.organization_id,
        signed_by_user_id=current_user.user_id,
        signed_by_name=waiver_signer_name,
        waiver_version=waiver_version,
        signed_at=now_iso,
        accepted=True,
    )

    payment = {
        "PAYMENT_ID": payment_id,
        "ORDER_ID": order_id,
        "PROVIDER": (
            "stripe"
            if payment_type == "STRIPE"
            else ("invoice" if payment_type == "INVOICE" else "none")
        ),
        "STRIPE_CHECKOUT_SESSION_ID": "",
        "STRIPE_PAYMENT_INTENT_ID": "",
        "PAYMENT_METHOD": "" if payment_type == "STRIPE" else payment_type,
        "AMOUNT": f"{total_amount:.2f}",
        "PROCESSING_FEE": "",
        "DISPLAY_STATUS": payment_status,
        "STRIPE_STATUS": technical_payment_status,
        "CREATED_AT": now_iso,
        "PAID_AT": now_iso if payment_status == "NO_COST" else "",
        "LAST_ATTEMPT_AT": "",
        "FAILURE_REASON": "",
    }

    return {
        "order": order,
        "registrations": registrations,
        "event_entries": event_entries,
        "waiver": waiver,
        "payment": payment,
    }


def _queue_clear_athlete_form() -> None:
    """Clear athlete widgets safely on the next rerun."""
    _queue_clear_athlete_fields()


def _build_current_athlete_cart_item() -> dict:
    registration_id = _new_id("REG")

    full_name_for_order = (
        (st.session_state.get("full_name", "") or "").strip()
        or (db_name_override or typed_full_name)
    )
    unique_id_for_order = (
        (st.session_state.get("unique_id_override", "") or "").strip()
        or unique_id
    )

    entry_rows = []
    for selected_event in selected_events:
        event_code = dict(event_opts).get(selected_event, "")
        entry_rows.append(
            {
                "entry_id": _new_id("ENT"),
                "registration_id": registration_id,
                "payment_provider": "",
                "payment_status": configured_payment_status,
                "competition_id": selected_competition_id,
                "competition_name": selected_competition.competition_name,
                "registration_period": registration_period,
                "organization_id": current_organization.organization_id,
                "organization_type": current_organization.organization_type,
                "entry_fee": f"{selected_entry_fee:.2f}",
                "name": (db_name_override or typed_full_name),
                "full_name": full_name_for_order,
                "name_passport": (
                    st.session_state.get("name_passport", "") or ""
                ).strip(),
                "last_name": (last_name or "").strip(),
                "first_name": (first_name or "").strip(),
                "other_name": (other_name or "").strip(),
                "gender": gender,
                "birth_date": birth_date.isoformat() if birth_date else "",
                "ic_last4": ic_last4_norm,
                "unique_id": unique_id_for_order,
                "nationality": nationality,
                "singapore_pr": singapore_pr,
                "contact_number": (contact_number or "").strip(),
                "email": email_norm,
                "team_code": team_code,
                "team_name": team_name_row,
                "charge_code": charge_code,
                "po_to_be_sent": po_to_be_sent,
                "event_division": event_division,
                "season_best": season_best_by_event.get(selected_event, ""),
                "emergency_contact_name": (
                    emergency_contact_name or ""
                ).strip(),
                "emergency_contact_number": (
                    emergency_contact_number or ""
                ).strip(),
                "coach_full_name": (coach_full_name or "").strip(),
                "parq": parq,
                "event": selected_event,
                "event_code": event_code,
            }
        )

    subtotal = selected_entry_fee * Decimal(len(entry_rows))

    return {
        "registration_id": registration_id,
        "athlete_name": full_name_for_order,
        "athlete_email": email_norm,
        "division": event_division,
        "events": list(selected_events),
        "entry_rows": entry_rows,
        "fee_per_event": f"{selected_entry_fee:.2f}",
        "subtotal": f"{subtotal:.2f}",
    }


# ---------------- Multi-athlete cart ----------------
_add_col, _cart_hint_col = st.columns([1, 2])
with _add_col:
    add_to_cart_clicked = st.button(
        "Add athlete to cart",
        type="primary",
        disabled=not ready_to_add,
        use_container_width=True,
    )
with _cart_hint_col:
    st.caption(
        "Add one or more athletes, then review the combined order below before "
        "payment or submission."
    )

if add_to_cart_clicked:
    _uid_present = bool((unique_id or "").strip())
    _is_sgp_local = (
        str(nationality or "").strip().upper()
        in ("SGP", "SIN", "SG", "SINGAPORE")
    )
    _pr_local = bool(st.session_state.get("singapore_pr", False))
    ic_required = bool(_pr_local) or (
        bool(_is_sgp_local) and (not _uid_present)
    )

    missing_checks = [
        ("Name as per NRIC/Passport", (st.session_state.get("name_passport", "") or "").strip()),
        ("Birth Date", birth_date),
        ("Nationality", nationality),
        ("Email", email),
        ("Contact Number", contact_number),
    ]
    for _event_name in selected_events:
        missing_checks.append(
            (
                f"Season Best ({_event_name})",
                season_best_by_event.get(_event_name, ""),
            )
        )
    if ic_required:
        missing_checks.insert(1, ("IC last 4", ic_last4))

    missing = [
        field_name
        for field_name, field_value in missing_checks
        if not field_value
    ]

    if missing:
        st.error("Missing: " + ", ".join(missing))
    elif not gender_ok:
        st.error("Please select Gender (Male or Female).")
    elif not is_valid_email(email_norm):
        st.error("Please enter a valid email address.")
    elif ic_required and not is_valid_ic_last4(ic_last4_norm):
        st.error("IC last 4 must be 3 digits followed by 1 letter (e.g., 123A).")
    elif not selected_events:
        st.error("Please select at least one event.")
    else:
        cart_item = _build_current_athlete_cart_item()

        # One athlete should appear only once in a single order/cart. If the
        # user wants additional events for an athlete already in the cart,
        # remove that athlete and re-add them with all desired events selected.
        new_candidate = candidate_from_cart_item(cart_item)
        same_athlete_in_cart = any(
            _same_cart_athlete(
                new_candidate, candidate_from_cart_item(existing_item)
            )
            for existing_item in get_cart()
        )

        if same_athlete_in_cart:
            st.error(
                "This athlete is already in the current cart. Remove the "
                "existing athlete and re-add them with all required events "
                "selected so the order contains one athlete record."
            )
        else:
            rules_ok = _validate_cart_against_competition_rules(
                [cart_item],
                fresh=False,
                stage="cart-add check",
            )
            if rules_ok:
                try:
                    integrity_result = _check_cart_item_against_transactions(
                        cart_item,
                        ignore_order_id=str(
                            st.session_state.get("draft_order_id", "") or ""
                        ).strip(),
                        stage="cart-add check",
                    )
                except Exception as exc:
                    st.error(
                        "Unable to verify existing competition registrations. "
                        "The athlete was not added to the cart. Please retry. "
                        f"({type(exc).__name__}: {exc})"
                    )
                else:
                    if integrity_result.blocked:
                        _render_integrity_conflicts(integrity_result)
                    else:
                        try:
                            add_cart_item(
                                cart_item,
                                competition_id=selected_competition_id,
                            )
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            _queue_clear_athlete_form()
                            st.toast("Athlete added to cart.")
                            st.rerun()


st.divider()
st.subheader("Order Cart")

cart = get_cart()
if not cart:
    st.info("Your cart is empty. Add an athlete above to start an order.")
else:
    summary_rows = []
    for item in cart:
        summary_rows.append(
            {
                "Athlete": item.get("athlete_name", ""),
                "Division": item.get("division", ""),
                "Events": ", ".join(item.get("events", []) or []),
                "Entries": len(item.get("entry_rows", []) or []),
                "Fee / event": f"S${Decimal(str(item.get('fee_per_event', '0'))):.2f}",
                "Subtotal": f"S${Decimal(str(item.get('subtotal', '0'))):.2f}",
            }
        )

    st.dataframe(
        pd.DataFrame(summary_rows),
        use_container_width=True,
        hide_index=True,
    )

    for index, item in enumerate(cart, start=1):
        _row1, _row2 = st.columns([5, 1])
        _row1.caption(
            f"{index}. {item.get('athlete_name', '')} — "
            f"{', '.join(item.get('events', []) or [])}"
        )
        if _row2.button(
            "Remove",
            key=f"remove_cart_{item.get('cart_item_id', index)}",
            use_container_width=True,
        ):
            remove_cart_item(item.get("cart_item_id", ""))
            st.rerun()

    total_entries = cart_total_event_entries()
    total_amount = cart_total_amount()

    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("Athletes", len(cart))
    _m2.metric("Event entries", total_entries)
    _m3.metric("Order total", f"S${total_amount:.2f}")

    if st.button("Clear cart", key="clear_registration_cart"):
        clear_cart()
        st.session_state.pop("order_waiver_ok", None)
        st.session_state.pop("order_waiver_signer_name", None)
        st.rerun()

    st.markdown("#### Competition waiver")
    waiver_version = str(
        pilot_config.system_value(
            "DEFAULT_WAIVER_VERSION",
            "TEST_WAIVER_V1",
        )
        or ""
    ).strip()
    waiver_text = str(
        pilot_config.system_value("WAIVER_TEXT", "") or ""
    ).strip()

    if waiver_version:
        st.caption(f"Waiver version: {waiver_version}")
    else:
        st.error(
            "The waiver version is not configured. Contact SA Events before submitting."
        )

    if waiver_text:
        st.info(waiver_text)
    else:
        st.caption(
            "The final SAA liability-waiver wording has not yet been supplied in the "
            "clarified requirements. During testing, this screen records the "
            "acknowledgement against the configured waiver version without inventing "
            "additional legal wording."
        )

    default_waiver_signer = str(current_user.display_name or "").strip()
    if "order_waiver_signer_name" not in st.session_state:
        st.session_state["order_waiver_signer_name"] = default_waiver_signer

    waiver_signer_name = st.text_input(
        "Representative name for waiver acknowledgement",
        key="order_waiver_signer_name",
        help=(
            "The logged-in representative acknowledges the waiver for all athletes "
            "and entries in this order."
        ),
    )

    order_waiver_ok = st.checkbox(
        "I acknowledge the competition waiver for all athletes and entries in this order.",
        value=False,
        key="order_waiver_ok",
    )

    st.caption(
        "This acknowledgement applies to this order and this competition only. "
        "A new order requires a new acknowledgement."
    )
    st.caption(
        "The cart is held in this browser session until you submit the order."
    )

    is_no_cost_order = total_amount == Decimal("0.00")
    is_school_order = (
        current_organization.organization_type == "SCHOOL"
        and not is_no_cost_order
    )
    is_online_paid_order = (
        current_organization.organization_type in {"AFFILIATE", "ASSOCIATE"}
        and total_amount > Decimal("0.00")
    )

    if is_no_cost_order:
        st.info(
            "No-cost order: no Stripe payment or MOE invoice is required. "
            "The registration will be confirmed with NO_COST payment status."
        )
    elif is_school_order:
        st.info(
            "School order: entries will be confirmed now and billed through the "
            "post-event MOE invoice process."
        )
    else:
        st.info("Paid order: one Stripe payment will cover the entire cart.")

    try:
        normalised_waiver_signer, normalised_waiver_version = (
            validate_waiver_acknowledgement(
                accepted=bool(order_waiver_ok),
                signer_name=waiver_signer_name,
                waiver_version=waiver_version,
            )
        )
        waiver_ready = True
    except WaiverComplianceError:
        normalised_waiver_signer = normalise_signer_name(waiver_signer_name)
        normalised_waiver_version = str(waiver_version or "").strip()
        waiver_ready = False

    submit_disabled = not waiver_ready

    # ---------------- No-cost / school transactional submission ----------------
    if is_school_order or is_no_cost_order:
        submit_label = (
            "Submit school entries"
            if is_school_order
            else "Submit no-cost registration"
        )

        if st.button(
            submit_label,
            type="primary",
            disabled=submit_disabled,
        ):
            order_id = _draft_order_id()
            if not _validate_cart_against_competition_rules(
                cart,
                fresh=True,
                stage="pre-submit recheck",
                order_id=order_id,
            ):
                st.stop()
            if not _validate_cart_against_transactions(
                cart, ignore_order_id=order_id
            ):
                st.stop()
            payment_id = "PAY-" + order_id.removeprefix("ORD-")

            payment_type = "INVOICE" if is_school_order else "NO_COST"
            stakeholder_payment_status = (
                "REQUIRED" if is_school_order else "NO_COST"
            )
            technical_payment_status = (
                "invoice_pending" if is_school_order else "no_cost"
            )

            bundle = _build_transaction_bundle(
                order_id=order_id,
                payment_id=payment_id,
                payment_type=payment_type,
                order_status="CONFIRMED",
                registration_status="CONFIRMED",
                event_status="CONFIRMED",
                payment_status=stakeholder_payment_status,
                technical_payment_status=technical_payment_status,
                total_amount=total_amount,
                cart=cart,
                waiver_signer_name=normalised_waiver_signer,
                waiver_version=normalised_waiver_version,
            )

            # Keep the existing OUTPUT sheet as a compatibility projection for
            # downstream processes while the transaction sheets become the
            # system of record.
            now_iso = _iso_now()
            new_rows = []
            for row in cart_entry_rows():
                final_row = dict(row)
                final_row["order_id"] = order_id
                final_row["payment_id"] = payment_id
                final_row["order_created_at"] = now_iso
                final_row["submitted_by"] = current_user_email
                final_row["payment_provider"] = (
                    "invoice" if is_school_order else "none"
                )
                final_row["payment_status"] = stakeholder_payment_status
                final_row["order_status"] = "CONFIRMED"
                new_rows.append(final_row)

            try:
                store = _transaction_store()
                store.persist_order_bundle(**bundle)

                existing_order_ids = {
                    str(existing.get("order_id", "") or "")
                    for existing in st.session_state.entries
                }
                if order_id not in existing_order_ids:
                    st.session_state.entries.extend(new_rows)

                sync_entries_to_sheet(
                    st.session_state.entries,
                    sheet_url_or_id=st.session_state.get(
                        "output_sheet_url", ""
                    ),
                    worksheet=(
                        (
                            st.session_state.get("output_worksheet", "")
                            or ""
                        ).strip()
                        or None
                    ),
                )
            except (TransactionStoreError, Exception) as exc:
                st.error(
                    "Unable to submit order: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                clear_cart()
                st.session_state.pop("order_waiver_ok", None)
                st.session_state.pop("order_waiver_signer_name", None)
                st.success(
                    f"Order {order_id} submitted successfully with "
                    f"{total_entries} event entries."
                )
                st.caption(
                    "You can return to the registration screen and enter another "
                    "athlete for the same competition."
                )
                if st.button(
                    "← Back to registration",
                    type="primary",
                    key=f"back_to_registration_after_{payment_type.lower()}",
                ):
                    _start_another_registration()
                st.stop()

    # ---------------- Paid Affiliate / Associate transactional Stripe checkout ----------------
    elif is_online_paid_order:
        if st.button(
            "Proceed to payment",
            type="primary",
            disabled=submit_disabled,
        ):
            order_id = _draft_order_id()
            if not _validate_cart_against_competition_rules(
                cart,
                fresh=True,
                stage="pre-submit recheck",
                order_id=order_id,
            ):
                st.stop()
            if not _validate_cart_against_transactions(
                cart, ignore_order_id=order_id
            ):
                st.stop()
            payment_id = "PAY-" + order_id.removeprefix("ORD-")

            currency = str(
                st.secrets.get("STRIPE_CURRENCY", "sgd") or "sgd"
            ).strip().lower()

            if currency != "sgd":
                st.error(
                    "Stripe PayNow requires STRIPE_CURRENCY to be set to 'sgd'."
                )
                st.stop()

            stripe_secret_key = str(
                st.secrets.get("STRIPE_SECRET_KEY", "") or ""
            ).strip()
            public_app_url = str(
                st.secrets.get(
                    "PUBLIC_APP_URL",
                    "https://saapublicaccess.streamlit.app",
                )
                or ""
            ).strip()
            pending_sheet_url = str(
                st.secrets.get("PENDING_PAYMENT_SHEET_URL", "") or ""
            ).strip()
            pending_worksheet_name = str(
                st.secrets.get(
                    "PENDING_PAYMENT_WORKSHEET",
                    "PendingPayments",
                )
                or "PendingPayments"
            ).strip()

            if not stripe_secret_key:
                st.error(
                    "Stripe is not configured: STRIPE_SECRET_KEY is missing."
                )
                st.stop()

            if not pending_sheet_url:
                st.error(
                    "Pending-payment storage is not configured: "
                    "PENDING_PAYMENT_SHEET_URL is missing."
                )
                st.stop()

            # Persist the order before contacting Stripe. A retry uses the same
            # draft ORDER_ID, so interrupted multi-sheet writes are idempotent.
            bundle = _build_transaction_bundle(
                order_id=order_id,
                payment_id=payment_id,
                payment_type="STRIPE",
                order_status="PENDING_PAYMENT",
                registration_status="PENDING_PAYMENT",
                event_status="PENDING_PAYMENT",
                payment_status="REQUIRED",
                technical_payment_status="not_started",
                total_amount=total_amount,
                cart=cart,
                waiver_signer_name=normalised_waiver_signer,
                waiver_version=normalised_waiver_version,
            )

            entry_rows = []
            all_events = []
            athlete_names = []

            for item in cart:
                athlete_names.append(item.get("athlete_name", ""))
                all_events.extend(item.get("events", []) or [])
                for row in item.get("entry_rows", []) or []:
                    pending_row = dict(row)
                    pending_row["order_id"] = order_id
                    pending_row["payment_id"] = payment_id
                    pending_row["payment_provider"] = "stripe"
                    pending_row["payment_status"] = "PAYMENT_STARTED"
                    entry_rows.append(pending_row)

            amount_str = f"{total_amount:.2f}"
            order_description = (
                f"{selected_competition.competition_name}: "
                f"{len(cart)} athlete(s), {total_entries} event entry/entries"
            )

            try:
                store = _transaction_store()
                store.persist_order_bundle(**bundle)

                # Reuse an existing usable Stripe Checkout session for this
                # order. This closes the duplicate-session path caused by
                # retries/double-clicks while still allowing a replacement
                # after a genuinely failed/expired session.
                payment_row = store.find_first(
                    "PAYMENTS",
                    "ORDER_ID",
                    order_id,
                ) or {}
                existing_session_id = str(
                    payment_row.get("STRIPE_CHECKOUT_SESSION_ID", "") or ""
                ).strip()
                existing_stripe_status = str(
                    payment_row.get("STRIPE_STATUS", "") or ""
                ).strip().lower()
                force_new_checkout = existing_stripe_status in {
                    "failed",
                    "expired",
                    "async_payment_failed",
                    "session_expired",
                }

                checkout = get_or_create_registration_checkout(
                    secret_key=stripe_secret_key,
                    registration_id=order_id,
                    amount=amount_str,
                    currency=currency,
                    customer_email=(
                        normalize_email(billing_email)
                        or current_user_email
                    ),
                    description=order_description,
                    public_app_url=public_app_url,
                    existing_session_id=existing_session_id,
                    force_new=force_new_checkout,
                )

                google_client = create_google_client(
                    dict(st.secrets["gcp_service_account"])
                )
                pending_worksheet = get_pending_worksheet(
                    google_client,
                    pending_sheet_url,
                    pending_worksheet_name,
                )

                upsert_pending_registration(
                    worksheet=pending_worksheet,
                    registration_id=order_id,
                    login_email=current_user_email,
                    athlete_email=(
                        normalize_email(billing_email)
                        or current_user_email
                    ),
                    full_name=(
                        athlete_names[0]
                        if len(athlete_names) == 1
                        else f"{len(athlete_names)} athletes"
                    ),
                    team_name=current_organization.organization_name,
                    events=all_events,
                    entry_rows=entry_rows,
                    amount=amount_str,
                    currency=currency,
                    stripe_session_id=checkout["session_id"],
                )

                attempt_time = _iso_now()
                # Do not downgrade an already-paid transaction merely because
                # the user revisited the payment button while webhook processing
                # is completing.
                if checkout.get("payment_status") != "paid":
                    store.mark_payment_started(
                        order_id=order_id,
                        payment_id=payment_id,
                        stripe_session_id=checkout["session_id"],
                        attempted_at=attempt_time,
                        checkout_url=checkout.get("payment_url", ""),
                    )

            except Exception as exc:
                # If Stripe Checkout itself never started, stakeholder-facing
                # status remains REQUIRED. The technical failure is surfaced to
                # the user and can be retried with the same ORDER_ID.
                try:
                    if "store" in locals():
                        store.update_by_id(
                            "PAYMENTS",
                            payment_id,
                            {
                                "DISPLAY_STATUS": "REQUIRED",
                                "STRIPE_STATUS": "checkout_error",
                                "LAST_ATTEMPT_AT": _iso_now(),
                                "FAILURE_REASON": (
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            },
                        )
                except Exception:
                    pass

                st.error(
                    "Unable to start payment: "
                    f"{type(exc).__name__}: {exc}"
                )
                st.stop()

            st.session_state["pending_checkout"] = {
                "order_id": order_id,
                "registration_id": order_id,
                "payment_id": payment_id,
                "session_id": checkout["session_id"],
                "payment_url": checkout["payment_url"],
                "amount": amount_str,
                "currency": currency,
                "athlete_count": len(cart),
                "event_entry_count": total_entries,
            }

            st.rerun()


# -------- Public entry-only mode --------
# Existing/current entries, download buttons, and edit/delete controls are intentionally hidden.
# Registrations are written to the confirmed output sheet only by the Stripe webhook
# after successful payment.
st.caption(
    "Entry-only mode: add athletes to one competition cart, then submit the "
    "combined order. Paid orders are confirmed only after Stripe webhook "
    "verification."
)
