import datetime as dt

from signup.payment_recovery import (
    checkout_force_new,
    find_resumable_payment_orders,
    order_can_be_resumed,
    pending_stripe_order_in_scope,
)
from signup.transaction_store import TransactionSheetStore


def _order(**overrides):
    row = {
        "ORDER_ID": "ORD-1",
        "COMPETITION_ID": "COMP-1",
        "USER_ID": "USR-1",
        "ORGANIZATION_ID": "ORG-1",
        "ENTRY_COUNT": "1",
        "TOTAL_AMOUNT": "12.00",
        "PAYMENT_TYPE": "STRIPE",
        "STATUS": "PAYMENT_STARTED",
        "CREATED_AT": "2026-09-26T04:00:00+00:00",
        "EXPIRES_AT": "2026-09-29T04:00:00+00:00",
    }
    row.update(overrides)
    return row


def _payment(**overrides):
    row = {
        "PAYMENT_ID": "PAY-1",
        "ORDER_ID": "ORD-1",
        "PROVIDER": "stripe",
        "STRIPE_CHECKOUT_SESSION_ID": "cs_test_123",
        "STRIPE_CHECKOUT_URL": "",
        "AMOUNT": "12.00",
        "CURRENCY": "sgd",
        "DISPLAY_STATUS": "PAYMENT_STARTED",
        "STRIPE_STATUS": "checkout_created",
    }
    row.update(overrides)
    return row


def _registration(**overrides):
    row = {
        "REGISTRATION_ID": "REG-1",
        "ORDER_ID": "ORD-1",
        "ATHLETE_NAME": "Phase Six",
        "EMAIL": "athlete@example.com",
    }
    row.update(overrides)
    return row


def _entry(**overrides):
    row = {
        "ENTRY_ID": "ENT-1",
        "ORDER_ID": "ORD-1",
        "EVENT_NAME": "200m",
    }
    row.update(overrides)
    return row


def _now():
    return dt.datetime(2026, 9, 26, 5, 0, tzinfo=dt.timezone.utc)



def test_orders_only_prefilter_rejects_non_pending_or_other_users():
    assert pending_stripe_order_in_scope(
        _order(), user_id="USR-1", organization_id="ORG-1", now=_now()
    )
    assert not pending_stripe_order_in_scope(
        _order(STATUS="CONFIRMED"),
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert not pending_stripe_order_in_scope(
        _order(), user_id="USR-2", organization_id="ORG-1", now=_now()
    )

def test_pending_stripe_order_owned_by_logged_in_user_can_resume():
    assert order_can_be_resumed(
        _order(), _payment(), user_id="USR-1", organization_id="ORG-1", now=_now()
    )


def test_other_user_or_organisation_cannot_resume_order():
    assert not order_can_be_resumed(
        _order(), _payment(), user_id="USR-2", organization_id="ORG-1", now=_now()
    )
    assert not order_can_be_resumed(
        _order(), _payment(), user_id="USR-1", organization_id="ORG-2", now=_now()
    )


def test_confirmed_or_settled_order_is_not_resumable():
    assert not order_can_be_resumed(
        _order(STATUS="CONFIRMED"),
        _payment(),
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert not order_can_be_resumed(
        _order(),
        _payment(DISPLAY_STATUS="PAYMENT_COMPLETE"),
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )


def test_expired_order_is_not_resumable():
    assert not order_can_be_resumed(
        _order(EXPIRES_AT="2026-09-26T04:30:00+00:00"),
        _payment(),
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )


def test_recovery_record_contains_existing_order_not_a_new_transaction():
    rows = find_resumable_payment_orders(
        orders=[_order()],
        payments=[_payment()],
        registrations=[_registration()],
        event_entries=[_entry()],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["order_id"] == "ORD-1"
    assert row["payment_id"] == "PAY-1"
    assert row["stripe_session_id"] == "cs_test_123"
    assert row["amount"] == "12.00"
    assert row["athlete_names"] == ["Phase Six"]
    assert row["athlete_count"] == 1
    assert row["event_entry_count"] == 1
    assert row["customer_email"] == "athlete@example.com"


def test_recovery_records_are_most_recent_first():
    old = _order(ORDER_ID="ORD-OLD", CREATED_AT="2026-09-26T03:00:00+00:00")
    new = _order(ORDER_ID="ORD-NEW", CREATED_AT="2026-09-26T04:00:00+00:00")
    old_payment = _payment(PAYMENT_ID="PAY-OLD", ORDER_ID="ORD-OLD")
    new_payment = _payment(PAYMENT_ID="PAY-NEW", ORDER_ID="ORD-NEW")
    rows = find_resumable_payment_orders(
        orders=[old, new],
        payments=[old_payment, new_payment],
        registrations=[],
        event_entries=[],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert [row["order_id"] for row in rows] == ["ORD-NEW", "ORD-OLD"]


def test_failed_expired_or_checkout_error_session_forces_replacement():
    assert checkout_force_new("failed")
    assert checkout_force_new("expired")
    assert checkout_force_new("session_expired")
    assert checkout_force_new("async_payment_failed")
    assert checkout_force_new("checkout_error")
    assert not checkout_force_new("checkout_created")


def test_mark_payment_started_persists_checkout_url_in_same_payment_update():
    store = object.__new__(TransactionSheetStore)
    calls = []

    def update_by_id(sheet, row_id, updates):
        calls.append(("update_by_id", sheet, row_id, updates))

    def update_where(sheet, column, value, updates):
        calls.append(("update_where", sheet, column, value, updates))

    store.update_by_id = update_by_id
    store.update_where = update_where

    store.mark_payment_started(
        order_id="ORD-1",
        payment_id="PAY-1",
        stripe_session_id="cs_test_123",
        attempted_at="2026-09-26T05:00:00+00:00",
        checkout_url="https://checkout.stripe.test/session",
    )

    payment_calls = [c for c in calls if c[0] == "update_by_id" and c[1] == "PAYMENTS"]
    assert len(payment_calls) == 1
    updates = payment_calls[0][3]
    assert updates["STRIPE_CHECKOUT_SESSION_ID"] == "cs_test_123"
    assert updates["STRIPE_CHECKOUT_URL"] == "https://checkout.stripe.test/session"
    assert updates["DISPLAY_STATUS"] == "PAYMENT_STARTED"
