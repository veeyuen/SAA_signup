from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import stripe


class StripeFinancialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class StripeBalanceFinancials:
    balance_transaction_id: str
    fee: Decimal
    net: Decimal
    currency: str


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _major(value: Any) -> Decimal:
    return Decimal(str(value or 0)) / Decimal("100")


def _balance_result(balance_transaction: Any) -> StripeBalanceFinancials:
    bt = _as_dict(balance_transaction)
    bt_id = _clean(bt.get("id"))
    if not bt_id:
        raise StripeFinancialsError("Stripe balance transaction is not available yet.")
    return StripeBalanceFinancials(
        balance_transaction_id=bt_id,
        fee=_major(bt.get("fee")),
        net=_major(bt.get("net")),
        currency=_clean(bt.get("currency")).upper(),
    )


def get_payment_intent_financials(
    *,
    secret_key: str,
    payment_intent_id: str,
) -> StripeBalanceFinancials:
    secret_key = _clean(secret_key)
    payment_intent_id = _clean(payment_intent_id)
    if not secret_key:
        raise StripeFinancialsError("STRIPE_SECRET_KEY is missing.")
    if not payment_intent_id:
        raise StripeFinancialsError("Stripe PaymentIntent ID is missing.")

    previous_key = getattr(stripe, "api_key", None)
    stripe.api_key = secret_key
    try:
        intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=["latest_charge.balance_transaction"],
        )
        intent = _as_dict(intent)
        charge = intent.get("latest_charge")
        if isinstance(charge, str) and charge:
            charge = stripe.Charge.retrieve(charge, expand=["balance_transaction"])
        charge = _as_dict(charge)
        if not charge:
            raise StripeFinancialsError(
                f"PaymentIntent {payment_intent_id} has no latest charge."
            )

        bt = charge.get("balance_transaction")
        if isinstance(bt, str) and bt:
            bt = stripe.BalanceTransaction.retrieve(bt)
        return _balance_result(bt)
    except StripeFinancialsError:
        raise
    except Exception as exc:
        raise StripeFinancialsError(
            f"Could not retrieve Stripe financials for PaymentIntent "
            f"{payment_intent_id}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        stripe.api_key = previous_key


def get_refund_financials(
    *,
    secret_key: str,
    stripe_refund_id: str,
) -> StripeBalanceFinancials:
    secret_key = _clean(secret_key)
    stripe_refund_id = _clean(stripe_refund_id)
    if not secret_key:
        raise StripeFinancialsError("STRIPE_SECRET_KEY is missing.")
    if not stripe_refund_id:
        raise StripeFinancialsError("Stripe refund ID is missing.")

    previous_key = getattr(stripe, "api_key", None)
    stripe.api_key = secret_key
    try:
        refund = stripe.Refund.retrieve(
            stripe_refund_id,
            expand=["balance_transaction"],
        )
        refund = _as_dict(refund)
        bt = refund.get("balance_transaction")
        if isinstance(bt, str) and bt:
            bt = stripe.BalanceTransaction.retrieve(bt)
        return _balance_result(bt)
    except StripeFinancialsError:
        raise
    except Exception as exc:
        raise StripeFinancialsError(
            f"Could not retrieve Stripe financials for refund "
            f"{stripe_refund_id}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        stripe.api_key = previous_key
