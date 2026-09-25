from __future__ import annotations

import html
import smtplib
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from typing import Any

import stripe


class FeeIncreasePaymentError(RuntimeError):
    pass


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value or {})


def _result(session: Any, *, reused: bool) -> dict:
    data = _as_dict(session)
    return {
        "session_id": str(data.get("id", "") or "").strip(),
        "payment_url": str(data.get("url", "") or "").strip(),
        "status": str(data.get("status", "") or "").strip().lower(),
        "payment_status": str(data.get("payment_status", "") or "").strip().lower(),
        "expires_at": data.get("expires_at", ""),
        "reused": reused,
    }


def get_or_create_fee_increase_checkout(
    *,
    secret_key: str,
    payment_id: str,
    order_id: str,
    entry_id: str,
    registration_id: str,
    amount: Decimal | str,
    currency: str,
    customer_email: str,
    athlete_name: str,
    event_name: str,
    original_entry_fee: Decimal | str,
    target_entry_fee: Decimal | str,
    public_app_url: str,
    existing_session_id: str = "",
    force_new: bool = False,
) -> dict:
    """Create or reuse the Stripe Checkout session for one fee increase.

    The deterministic idempotency key is based on the internal PAYMENT_ID. A
    genuine replacement after an expired/failed Checkout session incorporates
    the old session ID, so double clicks cannot create multiple live sessions.
    """
    secret_key = str(secret_key or "").strip()
    payment_id = str(payment_id or "").strip()
    order_id = str(order_id or "").strip()
    entry_id = str(entry_id or "").strip()
    registration_id = str(registration_id or "").strip()
    existing_session_id = str(existing_session_id or "").strip()
    currency = str(currency or "SGD").strip().lower()
    public_app_url = str(public_app_url or "").strip().rstrip("/")

    if not secret_key:
        raise FeeIncreasePaymentError("STRIPE_SECRET_KEY is missing.")
    if not payment_id or not order_id or not entry_id:
        raise FeeIncreasePaymentError("PAYMENT_ID, ORDER_ID and ENTRY_ID are required.")
    if currency != "sgd":
        raise FeeIncreasePaymentError("Fee-increase Checkout currently supports SGD only.")
    if not public_app_url:
        raise FeeIncreasePaymentError("PUBLIC_APP_URL is missing.")

    try:
        amount_decimal = Decimal(str(amount or "0")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        original_fee = Decimal(str(original_entry_fee or "0")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        target_fee = Decimal(str(target_entry_fee or "0")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except Exception as exc:
        raise FeeIncreasePaymentError(f"Invalid fee amount: {exc}") from exc

    if amount_decimal <= 0:
        raise FeeIncreasePaymentError("Additional payment amount must be greater than zero.")
    if target_fee <= original_fee:
        raise FeeIncreasePaymentError("TARGET_ENTRY_FEE must be greater than ORIGINAL_ENTRY_FEE.")
    if (target_fee - original_fee) != amount_decimal:
        raise FeeIncreasePaymentError(
            "Additional payment must equal TARGET_ENTRY_FEE - ORIGINAL_ENTRY_FEE."
        )

    stripe.api_key = secret_key

    retry_basis = "initial"
    if existing_session_id:
        retry_basis = existing_session_id
        if not force_new:
            try:
                existing = stripe.checkout.Session.retrieve(existing_session_id)
                data = _as_dict(existing)
                status = str(data.get("status", "") or "").lower()
                payment_status = str(data.get("payment_status", "") or "").lower()
                url = str(data.get("url", "") or "").strip()
                if status == "open" and url:
                    return _result(existing, reused=True)
                if status == "complete" or payment_status == "paid":
                    return _result(existing, reused=True)
            except stripe.error.InvalidRequestError:
                pass
            except Exception as exc:
                raise FeeIncreasePaymentError(
                    f"Could not inspect existing Stripe Checkout session: {exc}"
                ) from exc

    unit_amount = int((amount_decimal * 100).to_integral_value())
    success_url = (
        f"{public_app_url}?payment_result=success"
        "&payment_adjustment=fee_increase"
        "&session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = (
        f"{public_app_url}?payment_result=cancelled"
        "&payment_adjustment=fee_increase"
    )

    safe_basis = retry_basis.replace("/", "_")[-100:]
    idempotency_key = f"saa-fee-increase-{payment_id}-{safe_basis}"

    description = (
        f"SAA registration fee adjustment — {athlete_name or 'athlete'}"
        + (f" — {event_name}" if event_name else "")
    )[:127]

    metadata = {
        "payment_purpose": "FEE_INCREASE",
        "payment_id": payment_id,
        "order_id": order_id,
        "entry_id": entry_id,
        "registration_id": registration_id,
        "original_entry_fee": f"{original_fee:.2f}",
        "target_entry_fee": f"{target_fee:.2f}",
    }

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "paynow"],
            client_reference_id=order_id,
            customer_email=str(customer_email or "").strip() or None,
            metadata=metadata,
            payment_intent_data={"metadata": metadata},
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": unit_amount,
                        "product_data": {"name": description},
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise FeeIncreasePaymentError(
            f"Stripe Checkout could not be created: {type(exc).__name__}: {exc}"
        ) from exc

    return _result(session, reused=False)


def send_fee_increase_payment_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_from: str,
    to_email: str,
    athlete_name: str,
    event_name: str,
    order_id: str,
    entry_id: str,
    original_entry_fee: str,
    target_entry_fee: str,
    amount_due: str,
    currency: str,
    payment_url: str,
    reason: str,
) -> None:
    host = str(smtp_host or "").strip()
    user = str(smtp_user or "").strip()
    password = str(smtp_password or "").strip()
    sender = str(smtp_from or user).strip()
    recipient = str(to_email or "").strip()
    payment_url = str(payment_url or "").strip()

    if not host or not user or not password:
        raise FeeIncreasePaymentError("SMTP_HOST, SMTP_USER and SMTP_PASS are required.")
    if not recipient:
        raise FeeIncreasePaymentError("Participant email is missing.")
    if not payment_url:
        raise FeeIncreasePaymentError("Stripe Checkout URL is missing.")

    subject = "Singapore Athletics registration fee adjustment"
    text_body = (
        "Dear Participant,\n\n"
        "Singapore Athletics has amended the fee for one of your registration entries.\n\n"
        f"Athlete: {athlete_name}\n"
        f"Event: {event_name}\n"
        f"Order: {order_id}\n"
        f"Entry: {entry_id}\n"
        f"Previous fee: {currency} {original_entry_fee}\n"
        f"Revised fee: {currency} {target_entry_fee}\n"
        f"Additional amount due: {currency} {amount_due}\n"
        f"Reason: {reason}\n\n"
        f"Please complete the additional payment here:\n{payment_url}\n\n"
        "The revised fee will take effect only after Stripe confirms payment.\n\n"
        "SAA\n"
    )

    html_body = f"""<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
<p>Dear Participant,</p>
<p>Singapore Athletics has amended the fee for one of your registration entries.</p>
<p>
Athlete: {html.escape(athlete_name)}<br>
Event: {html.escape(event_name)}<br>
Order: {html.escape(order_id)}<br>
Entry: {html.escape(entry_id)}<br>
Previous fee: {html.escape(currency)} {html.escape(original_entry_fee)}<br>
Revised fee: {html.escape(currency)} {html.escape(target_entry_fee)}<br>
<strong>Additional amount due: {html.escape(currency)} {html.escape(amount_due)}</strong>
</p>
<p><strong>Reason:</strong> {html.escape(reason)}</p>
<p><a href="{html.escape(payment_url, quote=True)}">Pay the additional amount with Stripe</a></p>
<p>The revised fee will take effect only after Stripe confirms payment.</p>
<p>SAA</p>
</body></html>"""

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(host, int(smtp_port or 587), timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:
        raise FeeIncreasePaymentError(
            f"Additional-payment email failed: {type(exc).__name__}: {exc}"
        ) from exc
