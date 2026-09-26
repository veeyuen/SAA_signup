from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import stripe


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value or {})


def _checkout_result(session: Any, *, reused: bool) -> dict:
    data = _as_dict(session)
    return {
        "session_id": str(data.get("id", "") or "").strip(),
        "payment_url": str(data.get("url", "") or "").strip(),
        "status": str(data.get("status", "") or "").strip().lower(),
        "payment_status": str(data.get("payment_status", "") or "").strip().lower(),
        "expires_at": data.get("expires_at", ""),
        "reused": reused,
    }


def get_or_create_registration_checkout(
    *,
    secret_key: str,
    registration_id: str,
    amount: str,
    currency: str,
    customer_email: str,
    description: str,
    public_app_url: str,
    existing_session_id: str = "",
    force_new: bool = False,
) -> dict:
    """Reuse an existing usable Checkout Session, otherwise create one idempotently.

    The deterministic idempotency key prevents rapid retries/double clicks from
    creating multiple live Checkout Sessions for the same order. If a previous
    session has genuinely expired/failed, its session ID becomes part of the
    retry key so a replacement session can be created safely.
    """
    secret_key = str(secret_key or "").strip()
    registration_id = str(registration_id or "").strip()
    existing_session_id = str(existing_session_id or "").strip()
    currency = str(currency or "sgd").strip().lower()
    public_app_url = str(public_app_url or "").strip().rstrip("/")

    if not secret_key:
        raise ValueError("Stripe secret key is missing.")
    if not registration_id:
        raise ValueError("registration_id/order_id is required.")
    if currency != "sgd":
        raise ValueError("PayNow Checkout requires SGD currency.")
    if not public_app_url:
        raise ValueError("PUBLIC_APP_URL is required.")

    stripe.api_key = secret_key

    retry_basis = "initial"
    if existing_session_id:
        retry_basis = existing_session_id
        if not force_new:
            try:
                existing = stripe.checkout.Session.retrieve(existing_session_id)
                existing_data = _as_dict(existing)
                existing_status = str(existing_data.get("status", "") or "").lower()
                payment_status = str(
                    existing_data.get("payment_status", "") or ""
                ).lower()
                url = str(existing_data.get("url", "") or "").strip()

                # An open session can safely be reused. A completed session is
                # also not replaced: paid/async settlement must be handled by
                # the authoritative Stripe webhook rather than creating a new
                # chance to pay the same order twice.
                if existing_status == "open" and url:
                    return _checkout_result(existing, reused=True)
                if existing_status == "complete" or payment_status == "paid":
                    return _checkout_result(existing, reused=True)
            except stripe.error.InvalidRequestError:
                # If the stored session can no longer be retrieved, create a
                # replacement with a retry-specific idempotency key.
                pass

    amount_decimal = Decimal(str(amount or "0")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if amount_decimal <= 0:
        raise ValueError("Checkout amount must be greater than zero.")
    unit_amount = int((amount_decimal * 100).to_integral_value())

    success_url = (
        f"{public_app_url}?payment_result=success"
        "&session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = f"{public_app_url}?payment_result=cancelled"

    # The first attempt is stable per order. A genuine retry is stable per
    # previous session, preventing two replacement sessions from a double click.
    safe_basis = retry_basis.replace("/", "_")[-120:]
    idempotency_key = f"saa-checkout-{registration_id}-{safe_basis}"

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card", "paynow"],
        client_reference_id=registration_id,
        customer_email=str(customer_email or "").strip() or None,
        metadata={"registration_id": registration_id},
        line_items=[
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": str(description or "SAA competition registration")[:127]
                    },
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        idempotency_key=idempotency_key,
    )

    return _checkout_result(session, reused=False)


def expire_registration_checkout(
    *,
    secret_key: str,
    session_id: str,
) -> dict:
    """Expire an unpaid open Checkout Session before cancelling its order.

    A completed/paid session is never expired and returns ``can_cancel=False``.
    This is deliberately fail-closed: if Stripe cannot verify the session, the
    caller receives the exception and must not cancel the local transaction.
    """
    secret_key = str(secret_key or "").strip()
    session_id = str(session_id or "").strip()
    if not secret_key:
        raise ValueError("Stripe secret key is missing.")
    if not session_id:
        raise ValueError("Stripe Checkout session ID is required.")

    stripe.api_key = secret_key
    session = stripe.checkout.Session.retrieve(session_id)
    data = _as_dict(session)
    status = str(data.get("status", "") or "").strip().lower()
    payment_status = str(data.get("payment_status", "") or "").strip().lower()

    if status == "complete" or payment_status in {"paid", "no_payment_required"}:
        return {
            "session_id": session_id,
            "status": status,
            "payment_status": payment_status,
            "can_cancel": False,
            "expired": False,
        }

    if status == "expired":
        return {
            "session_id": session_id,
            "status": status,
            "payment_status": payment_status,
            "can_cancel": True,
            "expired": True,
        }

    if status != "open":
        raise RuntimeError(
            "Stripe Checkout session is in an unexpected state "
            f"({status or 'unknown'} / {payment_status or 'unknown'}). "
            "The registration was not cancelled."
        )

    try:
        expired_session = stripe.checkout.Session.expire(session_id)
    except stripe.error.InvalidRequestError:
        # A payment may have completed in the narrow window between retrieve
        # and expire. Re-read once and fail closed if it is now complete/paid.
        latest = stripe.checkout.Session.retrieve(session_id)
        latest_data = _as_dict(latest)
        latest_status = str(latest_data.get("status", "") or "").strip().lower()
        latest_payment = str(
            latest_data.get("payment_status", "") or ""
        ).strip().lower()
        if latest_status == "complete" or latest_payment in {
            "paid",
            "no_payment_required",
        }:
            return {
                "session_id": session_id,
                "status": latest_status,
                "payment_status": latest_payment,
                "can_cancel": False,
                "expired": False,
            }
        raise

    expired_data = _as_dict(expired_session)
    return {
        "session_id": session_id,
        "status": str(expired_data.get("status", "expired") or "expired").strip().lower(),
        "payment_status": str(
            expired_data.get("payment_status", payment_status) or payment_status
        ).strip().lower(),
        "can_cancel": True,
        "expired": True,
    }
