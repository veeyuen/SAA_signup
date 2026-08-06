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
from webhook_email import send_paid_confirmation_email
from webhook_output_writer import (
    append_confirmed_entries_if_missing,
)


def _json_env(name: str) -> dict:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        raise RuntimeError(f"{name} is missing.")
    return json.loads(raw)


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

    event_type = str(event.get("type", "") or "")
    session = event["data"]["object"]

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

    registration_id = str(
        session.get("client_reference_id")
        or session.get("metadata", {}).get("registration_id")
        or ""
    ).strip()

    if not registration_id:
        return jsonify(
            {"error": "Missing registration ID"}
        ), 400

    google_client = create_google_client(
        _json_env("GCP_SERVICE_ACCOUNT_JSON")
    )

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
        registration_id,
    )

    if not pending or not row_number:
        return jsonify(
            {"error": "Pending registration not found"}
        ), 404

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
    actual_session_id = str(session.get("id", "") or "").strip()

    if (
        expected_session_id
        and actual_session_id != expected_session_id
    ):
        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=(
                "Stripe session mismatch: expected "
                f"{expected_session_id}, received {actual_session_id}"
            ),
        )
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
        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=(
                f"Amount mismatch: expected {expected_amount}, "
                f"received {actual_amount}"
            ),
        )
        return jsonify({"error": "Amount mismatch"}), 400

    if actual_currency != expected_currency:
        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=(
                f"Currency mismatch: expected {expected_currency}, "
                f"received {actual_currency}"
            ),
        )
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
            registration_id=registration_id,
            entry_rows=entry_rows,
            stripe_session_id=actual_session_id,
            stripe_payment_intent_id=payment_intent_id,
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="PAID",
            stripe_payment_intent_id=payment_intent_id,
            confirmed_at=dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
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
            registration_id=registration_id,
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
            "registration_id": registration_id,
            "status": "PAID",
        }
    ), 200
