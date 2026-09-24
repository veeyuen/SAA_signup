from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal

import stripe
from flask import Request, jsonify

from payment_store import (
    create_google_client,
    find_pending_registration,
    get_pending_worksheet,
    update_pending_fields,
)
from transaction_store import (
    TransactionSheetStore,
    TransactionStoreError,
)
from webhook_email import send_paid_confirmation_email
from webhook_output_writer import (
    append_confirmed_entries_if_missing,
)


def _json_env(name: str) -> dict:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        raise RuntimeError(f"{name} is missing.")
    return json.loads(raw)


def _transaction_sheet_url() -> str:
    # For the current pilot, transaction tabs live in the same workbook as
    # OUTPUT. TRANSACTION_SHEET_URL can override this later.
    return str(
        os.environ.get("TRANSACTION_SHEET_URL", "")
        or os.environ.get("OUTPUT_SHEET_URL", "")
        or ""
    ).strip()


def _payment_method_from_session(session) -> str:
    methods = session.get("payment_method_types") or []
    if isinstance(methods, (list, tuple)):
        return ",".join(str(value) for value in methods if value)
    return str(methods or "").strip()


def _mark_transaction_failure(
    *,
    store: TransactionSheetStore | None,
    stripe_session_id: str,
    stripe_status: str,
    failure_reason: str,
) -> tuple[bool, str]:
    if store is None:
        return True, ""

    try:
        store.mark_payment_failed_by_stripe_session(
            stripe_session_id=stripe_session_id,
            failed_at=dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
            stripe_status=stripe_status,
            failure_reason=failure_reason,
        )
        return True, ""
    except TransactionStoreError as exc:
        return False, str(exc)


def stripe_webhook(request: Request):
    raw_body = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    webhook_secret = str(
        os.environ.get("STRIPE_WEBHOOK_SECRET", "") or ""
    ).strip()

    if not webhook_secret:
        return jsonify(
            {"error": "STRIPE_WEBHOOK_SECRET is missing"}
        ), 500

    try:
        event = stripe.Webhook.construct_event(
            raw_body,
            signature,
            webhook_secret,
        )
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400

    # Recent stripe-python versions return Stripe Event/StripeObject
    # instances rather than plain dictionaries. Convert them before
    # using dict-style access such as .get().
    if not isinstance(event, dict):
        if hasattr(event, "to_dict_recursive"):
            event = event.to_dict_recursive()
        elif hasattr(event, "to_dict"):
            event = event.to_dict()

    event_type = str(event.get("type", "") or "")
    session = event["data"]["object"]

    if not isinstance(session, dict):
        if hasattr(session, "to_dict_recursive"):
            session = session.to_dict_recursive()
        elif hasattr(session, "to_dict"):
            session = session.to_dict()

    handled_success_events = {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }
    handled_failure_events = {
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }

    if event_type not in (
        handled_success_events | handled_failure_events
    ):
        return jsonify({"received": True}), 200

    reference_id = str(
        session.get("client_reference_id")
        or session.get("metadata", {}).get("registration_id")
        or ""
    ).strip()

    if not reference_id:
        return jsonify(
            {"error": "Missing registration/order ID"}
        ), 400

    # New Phase 2B orders use the ORDER_ID as Stripe's client_reference_id.
    # Older pending rows may still have a legacy registration reference.
    order_id = reference_id
    is_transaction_order = order_id.upper().startswith("ORD-")

    google_client = create_google_client(
        _json_env("GCP_SERVICE_ACCOUNT_JSON")
    )

    transaction_store = None
    if is_transaction_order:
        transaction_sheet_url = _transaction_sheet_url()
        if not transaction_sheet_url:
            return jsonify(
                {
                    "error":
                    "TRANSACTION_SHEET_URL/OUTPUT_SHEET_URL is missing"
                }
            ), 500

        try:
            transaction_store = TransactionSheetStore(
                google_client=google_client,
                sheet_url=transaction_sheet_url,
            )
        except TransactionStoreError as exc:
            return jsonify(
                {"error": f"Transaction store unavailable: {exc}"}
            ), 500

    pending_worksheet = get_pending_worksheet(
        google_client,
        os.environ["PENDING_PAYMENT_SHEET_URL"],
        os.environ.get(
            "PENDING_PAYMENT_WORKSHEET",
            "PendingPayments",
        ),
    )

    row_number, pending = find_pending_registration(
        pending_worksheet,
        reference_id,
    )

    if not pending or not row_number:
        return jsonify(
            {"error": "Pending registration/order not found"}
        ), 404

    actual_session_id = str(session.get("id", "") or "").strip()

    # ------------------------------------------------------------------
    # Explicit Stripe failure / expiry
    # ------------------------------------------------------------------
    if event_type in handled_failure_events:
        failure_status = (
            "EXPIRED"
            if event_type == "checkout.session.expired"
            else "FAILED"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status=failure_status,
            error=event_type,
        )

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status=failure_status.lower(),
            failure_reason=event_type,
        )
        if not ok:
            # 500 causes Stripe to retry. All transaction-store updates are
            # idempotent, so retries are safe.
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify(
            {"received": True, "status": failure_status}
        ), 200

    # For card payments this is normally paid at checkout.session.completed.
    # If the method is asynchronous, wait for async_payment_succeeded.
    if str(session.get("payment_status", "")).lower() != "paid":
        return jsonify(
            {"received": True, "paid": False}
        ), 200

    expected_session_id = str(
        pending.get("stripe_checkout_session_id", "") or ""
    ).strip()

    if (
        expected_session_id
        and actual_session_id != expected_session_id
    ):
        mismatch_reason = (
            "Stripe session mismatch: expected "
            f"{expected_session_id}, received {actual_session_id}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=mismatch_reason,
        )

        # The transaction row contains the expected session ID, not the
        # mismatched incoming one.
        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=expected_session_id,
            stripe_status="session_mismatch",
            failure_reason=mismatch_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify({"error": "Session mismatch"}), 400

    expected_amount = Decimal(
        str(pending.get("amount", "0"))
    )
    actual_amount = (
        Decimal(str(session.get("amount_total", 0)))
        / Decimal("100")
    )

    expected_currency = str(
        pending.get("currency", "")
    ).lower()
    actual_currency = str(
        session.get("currency", "")
    ).lower()

    if actual_amount != expected_amount:
        amount_reason = (
            f"Amount mismatch: expected {expected_amount}, "
            f"received {actual_amount}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=amount_reason,
        )

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status="amount_mismatch",
            failure_reason=amount_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify({"error": "Amount mismatch"}), 400

    if actual_currency != expected_currency:
        currency_reason = (
            f"Currency mismatch: expected {expected_currency}, "
            f"received {actual_currency}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=currency_reason,
        )

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status="currency_mismatch",
            failure_reason=currency_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify({"error": "Currency mismatch"}), 400

    payment_intent_id = str(
        session.get("payment_intent", "") or ""
    )
    current_status = str(
        pending.get("status", "") or ""
    ).upper()
    ack_email_sent = str(
        pending.get("ack_email_sent", "") or ""
    ).strip().lower() in ("yes", "y", "true", "1")

    confirmed_at = dt.datetime.now(
        dt.timezone.utc
    ).isoformat()

    # ------------------------------------------------------------------
    # Authoritative transaction-table confirmation
    # ------------------------------------------------------------------
    # Run this even if PendingPayments already says PAID. If a previous
    # webhook invocation stopped after the legacy pending update, a Stripe
    # retry will repair the transaction tables.
    if transaction_store is not None:
        try:
            transaction_store.mark_payment_complete_by_stripe_session(
                stripe_session_id=actual_session_id,
                stripe_payment_intent_id=payment_intent_id,
                paid_at=confirmed_at,
                payment_method=_payment_method_from_session(session),
                processing_fee="",
            )
        except TransactionStoreError as exc:
            return jsonify(
                {"error": f"Transaction completion failed: {exc}"}
            ), 500

    # ------------------------------------------------------------------
    # Existing legacy OUTPUT projection + PendingPayments state
    # ------------------------------------------------------------------
    if current_status != "PAID":
        entry_rows = json.loads(
            str(pending.get("entry_rows_json", "[]") or "[]")
        )

        append_confirmed_entries_if_missing(
            gc=google_client,
            output_sheet_url_or_id=os.environ[
                "OUTPUT_SHEET_URL"
            ],
            output_worksheet=os.environ.get(
                "OUTPUT_WORKSHEET",
                "",
            ),
            order_id=order_id,
            entry_rows=entry_rows,
            stripe_session_id=actual_session_id,
            stripe_payment_intent_id=payment_intent_id,
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="PAID",
            stripe_payment_intent_id=payment_intent_id,
            confirmed_at=confirmed_at,
            error="",
        )

    # Email can be retried independently if Stripe retries the webhook.
    if not ack_email_sent:
        events = json.loads(
            str(pending.get("events_json", "[]") or "[]")
        )

        send_paid_confirmation_email(
            to_email=str(
                pending.get("athlete_email", "") or ""
            ).strip(),
            full_name=str(
                pending.get("full_name", "") or ""
            ).strip(),
            team_name=str(
                pending.get("team_name", "") or ""
            ).strip(),
            events=events,
            registration_id=reference_id,
            amount=f"{expected_amount:.2f}",
            currency=expected_currency.upper(),
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            ack_email_sent="Yes",
        )

    return jsonify(
        {
            "received": True,
            "order_id": order_id if is_transaction_order else "",
            "registration_id": reference_id,
            "status": "PAID",
        }
    ), 200
