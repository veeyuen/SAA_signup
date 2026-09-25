from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import stripe


class StripeRefundError(RuntimeError):
    pass


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value or {})


def _amount_to_minor_units(amount: str | Decimal) -> int:
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise StripeRefundError("Refund amount is invalid.") from exc
    if value <= 0:
        raise StripeRefundError("Refund amount must be greater than zero.")
    return int((value * 100).to_integral_value())


def create_stripe_refund(
    *,
    secret_key: str,
    refund_id: str,
    payment_intent_id: str,
    amount: str | Decimal,
    currency: str,
    entry_id: str,
    registration_id: str,
    order_id: str,
    payment_id: str,
    approved_by_user_id: str,
    approved_at: str,
    refund_type: str = "WITHDRAWAL",
    original_entry_fee: str | Decimal = "",
    target_entry_fee: str | Decimal = "",
    refund_group_id: str = "",
    refund_sequence: str | int = "",
    refund_group_total: str | Decimal = "",
    source_payment_purpose: str = "",
) -> dict:
    """Create one Stripe refund idempotently for an approved SAA refund.

    The deterministic idempotency key means an operator can safely retry after
    a timeout or a Google Sheets write failure without creating a second refund.
    Stripe metadata lets the authoritative webhook reconcile the refund back to
    the correct internal REFUNDS/EVENT_ENTRIES rows even if the browser request
    fails after Stripe accepted the refund.
    """
    secret_key = str(secret_key or "").strip()
    refund_id = str(refund_id or "").strip()
    payment_intent_id = str(payment_intent_id or "").strip()
    currency = str(currency or "SGD").strip().lower()

    if not secret_key:
        raise StripeRefundError("STRIPE_SECRET_KEY is missing.")
    if not refund_id:
        raise StripeRefundError("REFUND_ID is required.")
    if not payment_intent_id:
        raise StripeRefundError(
            "The payment has no Stripe PaymentIntent ID, so it cannot be refunded automatically."
        )
    if currency != "sgd":
        raise StripeRefundError("This pilot refund flow currently supports SGD only.")

    amount_minor = _amount_to_minor_units(amount)
    previous_key = getattr(stripe, "api_key", None)
    stripe.api_key = secret_key
    try:
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=amount_minor,
            reason="requested_by_customer",
            metadata={
                "refund_id": refund_id,
                "entry_id": str(entry_id or "").strip(),
                "registration_id": str(registration_id or "").strip(),
                "order_id": str(order_id or "").strip(),
                "payment_id": str(payment_id or "").strip(),
                "approved_by_user_id": str(approved_by_user_id or "").strip(),
                "approved_at": str(approved_at or "").strip(),
                "refund_type": str(refund_type or "WITHDRAWAL").strip().upper(),
                "original_entry_fee": str(original_entry_fee or "").strip(),
                "target_entry_fee": str(target_entry_fee or "").strip(),
                "refund_group_id": str(refund_group_id or "").strip(),
                "refund_sequence": str(refund_sequence or "").strip(),
                "refund_group_total": str(refund_group_total or "").strip(),
                "source_payment_purpose": str(source_payment_purpose or "").strip(),
            },
            idempotency_key=f"saa-refund-{refund_id}",
        )
    except stripe.error.StripeError as exc:
        user_message = str(getattr(exc, "user_message", "") or "").strip()
        message = user_message or str(exc)
        raise StripeRefundError(message) from exc
    except Exception as exc:
        raise StripeRefundError(f"Stripe refund creation failed: {type(exc).__name__}: {exc}") from exc
    finally:
        stripe.api_key = previous_key

    data = _as_dict(refund)
    return {
        "refund_id": str(data.get("id", "") or "").strip(),
        "status": str(data.get("status", "") or "").strip().lower(),
        "amount_minor": int(data.get("amount", amount_minor) or amount_minor),
        "currency": str(data.get("currency", currency) or currency).strip().lower(),
        "payment_intent_id": str(data.get("payment_intent", payment_intent_id) or payment_intent_id).strip(),
        "failure_reason": str(data.get("failure_reason", "") or "").strip(),
    }
