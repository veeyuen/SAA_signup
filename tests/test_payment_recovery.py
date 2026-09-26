import datetime as dt

from signup.payment_recovery import (
    checkout_force_new,
    find_resumable_payment_orders,
    order_can_be_resumed,
    pending_stripe_order_in_scope,
)
from signup.transaction_store import TransactionSheetStore
from signup.pending_payment import (
    cancel_pending_registration,
    retarget_pending_checkout_session,
)


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

    payment_calls = [c for c in calls if c[0] == "update_where" and c[1] == "PAYMENTS"]
    assert len(payment_calls) == 1
    assert payment_calls[0][2] == "PAYMENT_ID"
    assert payment_calls[0][3] == "PAY-1"
    updates = payment_calls[0][4]
    assert updates["STRIPE_CHECKOUT_SESSION_ID"] == "cs_test_123"
    assert updates["STRIPE_CHECKOUT_URL"] == "https://checkout.stripe.test/session"
    assert updates["DISPLAY_STATUS"] == "PAYMENT_STARTED"



def test_paid_duplicate_payment_row_wins_over_stale_pending_duplicate():
    rows = find_resumable_payment_orders(
        orders=[_order()],
        payments=[
            _payment(PAYMENT_ID="PAY-OLD", DISPLAY_STATUS="PAYMENT_STARTED"),
            _payment(
                PAYMENT_ID="PAY-PAID",
                DISPLAY_STATUS="PAYMENT_COMPLETE",
                STRIPE_STATUS="paid",
                PAID_AT="2026-09-26T05:10:00+00:00",
            ),
        ],
        registrations=[_registration()],
        event_entries=[_entry()],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert rows == []


def test_confirmed_duplicate_order_row_wins_over_stale_pending_duplicate():
    rows = find_resumable_payment_orders(
        orders=[
            _order(STATUS="PAYMENT_STARTED"),
            _order(STATUS="CONFIRMED", UPDATED_AT="2026-09-26T05:10:00+00:00"),
        ],
        payments=[_payment()],
        registrations=[_registration()],
        event_entries=[_entry()],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert rows == []


def test_confirmed_child_payment_state_suppresses_resume_prompt():
    rows = find_resumable_payment_orders(
        orders=[_order()],
        payments=[_payment()],
        registrations=[_registration(STATUS="CONFIRMED")],
        event_entries=[
            _entry(
                STATUS="CONFIRMED",
                PAYMENT_STATUS="PAYMENT_COMPLETE",
                IS_DELETED="FALSE",
            )
        ],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert rows == []


def test_stale_duplicate_order_rows_are_collapsed_to_one_resume_candidate():
    rows = find_resumable_payment_orders(
        orders=[
            _order(CREATED_AT="2026-09-26T03:00:00+00:00"),
            _order(CREATED_AT="2026-09-26T04:00:00+00:00"),
        ],
        payments=[_payment()],
        registrations=[_registration()],
        event_entries=[_entry()],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert len(rows) == 1
    assert rows[0]["order_id"] == "ORD-1"
    assert rows[0]["duplicate_order_rows"] == 1


class _FakeWorksheet:
    def __init__(self, values):
        self.values = [list(row) for row in values]
        self.updates = []

    def get_all_values(self):
        return [list(row) for row in self.values]

    def update(self, *args, **kwargs):
        if kwargs:
            range_name = kwargs["range_name"]
            values = kwargs["values"]
        else:
            range_name, values = args
        row_number = int(range_name.split(":")[0][1:])
        self.values[row_number - 1] = list(values[0])
        self.updates.append((range_name, values))


def test_retarget_pending_checkout_session_updates_current_row_only():
    headers = [
        "registration_id",
        "status",
        "stripe_checkout_session_id",
        "error",
        "entry_rows_json",
    ]
    ws = _FakeWorksheet(
        [
            headers,
            ["ORD-1", "FAILED", "cs_old_1", "expired", "[{\"x\":1}]"],
            ["ORD-1", "FAILED", "cs_old_2", "expired", "[{\"x\":2}]"],
        ]
    )

    row_number = retarget_pending_checkout_session(
        worksheet=ws,
        registration_id="ORD-1",
        stripe_session_id="cs_new",
    )

    assert row_number == 3
    assert ws.values[1][2] == "cs_old_1"
    assert ws.values[2][1] == "PENDING"
    assert ws.values[2][2] == "cs_new"
    assert ws.values[2][3] == ""
    assert ws.values[2][4] == '[{"x":2}]'


def test_payment_complete_updates_all_duplicate_logical_ids():
    store = object.__new__(TransactionSheetStore)
    calls = []

    store.find_first = lambda *args: {
        "PAYMENT_ID": "PAY-1",
        "ORDER_ID": "ORD-1",
    }

    def update_where(sheet, column, value, updates):
        calls.append((sheet, column, value, updates))
        return 2

    store.update_where = update_where

    order_id = store.mark_payment_complete_by_stripe_session(
        stripe_session_id="cs_paid",
        stripe_payment_intent_id="pi_1",
        paid_at="2026-09-26T05:20:00+00:00",
    )

    assert order_id == "ORD-1"
    assert any(
        sheet == "PAYMENTS" and column == "PAYMENT_ID" and value == "PAY-1"
        for sheet, column, value, _ in calls
    )
    assert any(
        sheet == "ORDERS" and column == "ORDER_ID" and value == "ORD-1"
        for sheet, column, value, _ in calls
    )


def test_cancelled_payment_is_not_resumable_even_if_order_row_is_stale_pending():
    rows = find_resumable_payment_orders(
        orders=[_order(STATUS="PAYMENT_STARTED")],
        payments=[_payment(DISPLAY_STATUS="CANCELLED", STRIPE_STATUS="expired")],
        registrations=[_registration()],
        event_entries=[_entry()],
        user_id="USR-1",
        organization_id="ORG-1",
        now=_now(),
    )
    assert rows == []


def test_cancel_pending_registration_marks_all_duplicate_legacy_rows_cancelled():
    headers = [
        "registration_id",
        "status",
        "stripe_checkout_session_id",
        "error",
    ]
    ws = _FakeWorksheet(
        [
            headers,
            ["ORD-1", "PENDING", "cs_old", ""],
            ["ORD-1", "FAILED", "cs_new", "expired"],
            ["ORD-2", "PENDING", "cs_other", ""],
        ]
    )

    updated = cancel_pending_registration(
        worksheet=ws,
        registration_id="ORD-1",
    )

    assert updated == 2
    assert ws.values[1][1] == "CANCELLED"
    assert ws.values[2][1] == "CANCELLED"
    assert ws.values[1][3] == "Cancelled by registrant before payment"
    assert ws.values[2][3] == "Cancelled by registrant before payment"
    assert ws.values[3][1] == "PENDING"


def test_cancel_pending_registration_refuses_any_paid_legacy_row():
    headers = [
        "registration_id",
        "status",
        "stripe_checkout_session_id",
    ]
    ws = _FakeWorksheet(
        [
            headers,
            ["ORD-1", "PENDING", "cs_old"],
            ["ORD-1", "PAID", "cs_paid"],
        ]
    )

    try:
        cancel_pending_registration(worksheet=ws, registration_id="ORD-1")
        assert False, "Expected paid order cancellation to be refused"
    except RuntimeError as exc:
        assert "already marked PAID" in str(exc)

    assert ws.values[1][1] == "PENDING"
    assert ws.values[2][1] == "PAID"


def test_transaction_store_cancel_pending_order_updates_all_logical_rows():
    store = object.__new__(TransactionSheetStore)
    calls = []

    data = {
        "ORDERS": [_order()],
        "PAYMENTS": [_payment()],
        "REGISTRATIONS": [_registration(STATUS="PENDING_PAYMENT")],
        "EVENT_ENTRIES": [
            _entry(STATUS="PENDING_PAYMENT", PAYMENT_STATUS="PAYMENT_STARTED")
        ],
    }
    store.list_rows = lambda sheet: list(data[sheet])

    def update_where(sheet, column, value, updates):
        calls.append((sheet, column, value, updates))
        return 1

    store.update_where = update_where

    before = store.cancel_pending_stripe_order(
        order_id="ORD-1",
        payment_id="PAY-1",
        cancelled_at="2026-09-26T06:50:00+00:00",
    )

    assert before["ORDER_STATUS"] == "PAYMENT_STARTED"
    assert before["PAYMENT_STATUS"] == "PAYMENT_STARTED"
    assert any(
        sheet == "ORDERS" and updates["STATUS"] == "CANCELLED"
        for sheet, _, _, updates in calls
    )
    assert any(
        sheet == "PAYMENTS"
        and updates["DISPLAY_STATUS"] == "CANCELLED"
        and updates["STRIPE_STATUS"] == "expired"
        for sheet, _, _, updates in calls
    )
    assert any(
        sheet == "REGISTRATIONS" and updates["STATUS"] == "CANCELLED"
        for sheet, _, _, updates in calls
    )
    assert any(
        sheet == "EVENT_ENTRIES"
        and updates["STATUS"] == "CANCELLED"
        and updates["PAYMENT_STATUS"] == "CANCELLED"
        for sheet, _, _, updates in calls
    )


def test_transaction_store_cancel_refuses_paid_transaction_without_updates():
    store = object.__new__(TransactionSheetStore)
    calls = []
    data = {
        "ORDERS": [_order()],
        "PAYMENTS": [
            _payment(
                DISPLAY_STATUS="PAYMENT_COMPLETE",
                STRIPE_STATUS="paid",
                PAID_AT="2026-09-26T06:49:00+00:00",
            )
        ],
        "REGISTRATIONS": [_registration(STATUS="CONFIRMED")],
        "EVENT_ENTRIES": [
            _entry(STATUS="CONFIRMED", PAYMENT_STATUS="PAYMENT_COMPLETE")
        ],
    }
    store.list_rows = lambda sheet: list(data[sheet])
    store.update_where = lambda *args, **kwargs: calls.append((args, kwargs))

    try:
        store.cancel_pending_stripe_order(
            order_id="ORD-1",
            payment_id="PAY-1",
            cancelled_at="2026-09-26T06:50:00+00:00",
        )
        assert False, "Expected paid transaction cancellation to be refused"
    except Exception as exc:
        assert "settled" in str(exc).lower() or "completed payment" in str(exc).lower()

    assert calls == []
