from __future__ import annotations

import datetime as dt
from typing import Any, Iterable


PENDING_ORDER_STATUSES = {"PENDING_PAYMENT", "PAYMENT_STARTED"}
SETTLED_PAYMENT_STATUSES = {
    "PAYMENT_COMPLETE",
    "PAID",
    "SUCCEEDED",
    "NO_COST",
    "REFUNDED",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _parse_iso(value: Any) -> dt.datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _first(rows: Iterable[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    wanted = _text(value)
    for row in rows:
        if _text(row.get(key)) == wanted:
            return row
    return {}


def _rows_for(rows: Iterable[dict[str, Any]], key: str, value: str) -> list[dict[str, Any]]:
    wanted = _text(value)
    return [row for row in rows if _text(row.get(key)) == wanted]


def pending_stripe_order_in_scope(
    order: dict[str, Any],
    *,
    user_id: str,
    organization_id: str,
    now: dt.datetime | None = None,
) -> bool:
    """Cheap ORDERS-only prefilter used to avoid unnecessary worksheet reads."""
    if not order:
        return False
    if _text(order.get("USER_ID")) != _text(user_id):
        return False
    if _text(order.get("ORGANIZATION_ID")) != _text(organization_id):
        return False
    if _upper(order.get("PAYMENT_TYPE")) != "STRIPE":
        return False
    if _upper(order.get("STATUS")) not in PENDING_ORDER_STATUSES:
        return False

    expires_at = _parse_iso(order.get("EXPIRES_AT"))
    if expires_at is not None:
        check_time = now or dt.datetime.now(dt.timezone.utc)
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=dt.timezone.utc)
        if expires_at <= check_time.astimezone(dt.timezone.utc):
            return False
    return True


def order_can_be_resumed(
    order: dict[str, Any],
    payment: dict[str, Any],
    *,
    user_id: str,
    organization_id: str,
    now: dt.datetime | None = None,
) -> bool:
    """Return True only for the logged-in user's live Stripe payment order."""
    if not payment:
        return False
    if not pending_stripe_order_in_scope(
        order,
        user_id=user_id,
        organization_id=organization_id,
        now=now,
    ):
        return False
    if _upper(payment.get("DISPLAY_STATUS")) in SETTLED_PAYMENT_STATUSES:
        return False
    return True


def find_resumable_payment_orders(
    *,
    orders: Iterable[dict[str, Any]],
    payments: Iterable[dict[str, Any]],
    registrations: Iterable[dict[str, Any]],
    event_entries: Iterable[dict[str, Any]],
    user_id: str,
    organization_id: str,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Build safe display/recovery records for resumable persisted orders.

    The result contains only orders owned by the logged-in user and organisation.
    No Stripe call is made here; the caller retrieves/recreates Checkout only
    after the user explicitly chooses Resume payment.
    """
    order_rows = list(orders)
    payment_rows = list(payments)
    registration_rows = list(registrations)
    entry_rows = list(event_entries)

    results: list[dict[str, Any]] = []
    for order in order_rows:
        order_id = _text(order.get("ORDER_ID"))
        if not order_id:
            continue

        payment = _first(payment_rows, "ORDER_ID", order_id)
        if not order_can_be_resumed(
            order,
            payment,
            user_id=user_id,
            organization_id=organization_id,
            now=now,
        ):
            continue

        regs = _rows_for(registration_rows, "ORDER_ID", order_id)
        entries = _rows_for(entry_rows, "ORDER_ID", order_id)
        athlete_names = []
        for reg in regs:
            name = _text(reg.get("ATHLETE_NAME"))
            if name and name not in athlete_names:
                athlete_names.append(name)

        customer_email = ""
        for reg in regs:
            customer_email = _text(reg.get("EMAIL"))
            if customer_email:
                break

        results.append(
            {
                "order_id": order_id,
                "payment_id": _text(payment.get("PAYMENT_ID")),
                "competition_id": _text(order.get("COMPETITION_ID")),
                "amount": _text(payment.get("AMOUNT")) or _text(order.get("TOTAL_AMOUNT")),
                "currency": _text(payment.get("CURRENCY")) or "sgd",
                "stripe_session_id": _text(payment.get("STRIPE_CHECKOUT_SESSION_ID")),
                "stripe_checkout_url": _text(payment.get("STRIPE_CHECKOUT_URL")),
                "stripe_status": _text(payment.get("STRIPE_STATUS")).lower(),
                "display_status": _text(payment.get("DISPLAY_STATUS")),
                "customer_email": customer_email,
                "athlete_names": athlete_names,
                "athlete_count": len(regs),
                "event_entry_count": len(entries),
                "created_at": _text(order.get("CREATED_AT")),
                "expires_at": _text(order.get("EXPIRES_AT")),
            }
        )

    # Most recent first; ISO timestamps sort chronologically when consistently formatted.
    return sorted(results, key=lambda row: row.get("created_at", ""), reverse=True)


def checkout_force_new(stripe_status: str) -> bool:
    """Statuses persisted by webhook/payment code that require a replacement session."""
    return _text(stripe_status).lower() in {
        "failed",
        "expired",
        "async_payment_failed",
        "session_expired",
        "checkout_error",
    }
