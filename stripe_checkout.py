# stripe_checkout.py

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import stripe


def amount_to_cents(amount: str) -> int:
    value = Decimal(str(amount)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return int(value * 100)


def create_registration_checkout(
    *,
    secret_key: str,
    registration_id: str,
    amount: str,
    currency: str,
    customer_email: str,
    description: str,
    public_app_url: str,
) -> dict:
    stripe.api_key = secret_key

    app_url = public_app_url.rstrip("/")

    session = stripe.checkout.Session.create(
        mode="payment",

        # One Checkout page offers both payment methods.
        payment_method_types=["card", "paynow"],

        customer_email=customer_email,
        client_reference_id=registration_id,

        metadata={
            "registration_id": registration_id,
        },

        line_items=[
            {
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": amount_to_cents(amount),
                    "product_data": {
                        "name": "Event registration",
                        "description": description,
                    },
                },
                "quantity": 1,
            }
        ],

        success_url=(
            f"{app_url}"
            f"?payment_result=success"
            f"&session_id={{CHECKOUT_SESSION_ID}}"
        ),

        cancel_url=(
            f"{app_url}"
            f"?payment_result=cancelled"
            f"&registration_id={registration_id}"
        ),
    )

    if not session.url:
        raise RuntimeError("Stripe did not return a Checkout URL.")

    return {
        "session_id": session.id,
        "payment_url": session.url,
    }
