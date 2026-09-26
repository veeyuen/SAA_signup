from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Iterable


PENDING_ORDER_STATUSES = {"PENDING_PAYMENT", "PAYMENT_STARTED"}
SETTLED_PAYMENT_STATUSES = {
    "PAYMENT_COMPLETE",
    "PAID",
    "SUCCEEDED",
    "NO_COST",
    "REFUNDED",
    "CANCELLED",
}
SETTLED_ORDER_STATUSES = {
    "CONFIRMED",
    "PAID",
    "COMPLETED",
    "CANCELLED",
    "WITHDRAWN",
}
SETTLED_ENTRY_PAYMENT_STATUSES = {
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


def _sort_timestamp(row: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = _text(row.get(field))
        if value:
            return value
    return ""


def _rows_for(rows: Iterable[dict[str, Any]], key: str, value: str) -> list[dict[str, Any]]:
    wanted = _text(value)
    return [row for row in rows if _text(row.get(key)) == wanted]


def _payment_is_settled(payment: dict[str, Any]) -> bool:
    if not payment:
        return False
    if _upper(payment.get("DISPLAY_STATUS")) in SETTLED_PAYMENT_STATUSES:
        return True
    if _upper(payment.get("STRIPE_STATUS")) in {"PAID", "SUCCEEDED"}:
        return True
    if _text(payment.get("PAID_AT")):
        return True
    return False


def _order_is_settled(order: dict[str, Any]) -> bool:
    return bool(order) and _upper(order.get("STATUS")) in SETTLED_ORDER_STATUSES


def _base_registration_payments(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the original registration payment over later fee-increase children."""
    base: list[dict[str, Any]] = []
    for payment in payments:
        purpose = _upper(payment.get("PAYMENT_PURPOSE"))
        parent = _text(payment.get("PARENT_PAYMENT_ID"))
        if (not purpose or purpose in {"REGISTRATION", "ENTRY"}) and not parent:
            base.append(payment)
    return base or payments


def _authoritative_payment(payments: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the payment row that should govern Resume payment.

    Historical/retry data can contain duplicate rows for the same order. A paid
    row always wins over a stale pending duplicate. Otherwise use the most
    recently attempted base registration payment rather than the first worksheet
    row.
    """
    candidates = _base_registration_payments(payments)
    settled = [row for row in candidates if _payment_is_settled(row)]
    pool = settled or candidates
    if not pool:
        return {}
    return max(
        pool,
        key=lambda row: _sort_timestamp(
            row,
            "PAID_AT",
            "LAST_ATTEMPT_AT",
            "CREATED_AT",
            "REQUESTED_AT",
        ),
    )


def _authoritative_order(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the order row, preferring any terminal/confirmed duplicate."""
    settled = [row for row in orders if _order_is_settled(row)]
    pool = settled or orders
    if not pool:
        return {}
    return max(
        pool,
        key=lambda row: _sort_timestamp(row, "UPDATED_AT", "CREATED_AT"),
    )


def _children_prove_payment_complete(
    registrations: list[dict[str, Any]],
    event_entries: list[dict[str, Any]],
) -> bool:
    """Return True when the order's child rows already prove settlement.

    EVENT_ENTRIES.PAYMENT_STATUS is only moved to PAYMENT_COMPLETE by the
    payment-confirmation path. This protects the UI from offering Resume payment
    when a stale duplicate ORDERS/PAYMENTS row survives after confirmation.
    """
    live_entries = [
        row
        for row in event_entries
        if _upper(row.get("IS_DELETED")) not in {"TRUE", "1", "Y", "YES"}
    ]
    if not live_entries:
        return False

    if not all(
        _upper(row.get("PAYMENT_STATUS")) in SETTLED_ENTRY_PAYMENT_STATUSES
        for row in live_entries
    ):
        return False

    # A registration row may be absent in malformed legacy data. If present,
    # require it not to remain explicitly pending.
    for row in registrations:
        if _upper(row.get("STATUS")) in PENDING_ORDER_STATUSES:
            return False
    return True


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
    if _payment_is_settled(payment):
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

    Duplicate worksheet rows are collapsed by ORDER_ID. A confirmed order,
    settled payment, or child EVENT_ENTRY already marked PAYMENT_COMPLETE wins
    over stale pending duplicates, preventing a paid order from continuing to
    display Resume payment.
    """
    order_rows = list(orders)
    payment_rows = list(payments)
    registration_rows = list(registrations)
    entry_rows = list(event_entries)

    orders_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in order_rows:
        order_id = _text(order.get("ORDER_ID"))
        if order_id:
            orders_by_id[order_id].append(order)

    payments_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payment in payment_rows:
        order_id = _text(payment.get("ORDER_ID"))
        if order_id:
            payments_by_order[order_id].append(payment)

    results: list[dict[str, Any]] = []
    for order_id, duplicate_orders in orders_by_id.items():
        order = _authoritative_order(duplicate_orders)
        duplicate_payments = payments_by_order.get(order_id, [])
        payment = _authoritative_payment(duplicate_payments)
        regs = _rows_for(registration_rows, "ORDER_ID", order_id)
        entries = _rows_for(entry_rows, "ORDER_ID", order_id)

        # Confirmation evidence anywhere in the logical transaction must win
        # over a stale pending duplicate row.
        if any(_order_is_settled(row) for row in duplicate_orders):
            continue
        if any(_payment_is_settled(row) for row in _base_registration_payments(duplicate_payments)):
            continue
        if _children_prove_payment_complete(regs, entries):
            continue

        if not order_can_be_resumed(
            order,
            payment,
            user_id=user_id,
            organization_id=organization_id,
            now=now,
        ):
            continue

        athlete_names: list[str] = []
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
                "duplicate_order_rows": max(0, len(duplicate_orders) - 1),
                "payment_rows_for_order": len(duplicate_payments),
            }
        )

    return sorted(results, key=lambda row: row.get("created_at", ""), reverse=True)


def checkout_force_new(stripe_status: str) -> bool:
    """Statuses persisted by webhook/payment code that require a replacement session."""
    return _text(stripe_status).lower() in {
        "failed",
        "expired",
        "async_payment_failed",
        "session_expired",
        "checkout_error",
        "session_mismatch",
    }
