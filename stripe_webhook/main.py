from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal

import stripe
from flask import Request, jsonify

from payment_store import (
    create_google_client,
    get_pending_worksheet,
    update_pending_fields,
)
from transaction_store import (
    TransactionSheetStore,
    TransactionStoreError,
)
from webhook_email import send_paid_confirmation_email
from webhook_output_writer import (
    append_confirmed_entries_if_missing,
    update_entry_payment_status,
    update_entry_fee_and_payment_status,
)


def _json_env(name: str) -> dict:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        raise RuntimeError(f"{name} is missing.")
    return json.loads(raw)


def _transaction_sheet_url() -> str:
    # For the current pilot, transaction tabs live in the same workbook as
    # OUTPUT. TRANSACTION_SHEET_URL can override this later.
    return str(
        os.environ.get("TRANSACTION_SHEET_URL", "")
        or os.environ.get("OUTPUT_SHEET_URL", "")
        or ""
    ).strip()


def _normalise_header(value: str) -> str:
    import re

    return re.sub(
        r"[^a-z0-9]+",
        "_",
        str(value or "").strip().casefold(),
    ).strip("_")


def _find_pending_for_session(worksheet, reference_id: str, session_id: str):
    """Find the PendingPayments row for this order and exact Stripe session.

    Older test data can contain more than one row for the same ORDER_ID. Matching
    by session first prevents a webhook for one attempt from mutating another.
    """
    values = worksheet.get_all_values()
    if not values:
        return None, None

    headers = values[0]
    header_map = {_normalise_header(h): i for i, h in enumerate(headers)}
    reg_idx = header_map.get("registration_id")
    session_idx = header_map.get("stripe_checkout_session_id")
    if reg_idx is None:
        return None, None

    matches = []
    for row_number, row in enumerate(values[1:], start=2):
        reg_value = row[reg_idx] if reg_idx < len(row) else ""
        if str(reg_value or "").strip() != str(reference_id or "").strip():
            continue
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        record = {
            headers[i]: padded[i] if i < len(padded) else ""
            for i in range(len(headers))
        }
        matches.append((row_number, record))
        if session_idx is not None and session_id:
            stored_session = padded[session_idx] if session_idx < len(padded) else ""
            if str(stored_session or "").strip() == session_id:
                return row_number, record

    if not matches:
        return None, None

    # For a single current row, use it and let the explicit session-mismatch
    # guard below decide whether the event is valid. For legacy duplicates with
    # no exact match, use the newest row rather than an arbitrary first row.
    return matches[-1]


def _actual_payment_method(session: dict) -> str:
    """Return the payment method actually used (e.g. card or paynow).

    Checkout Session.payment_method_types is only the list offered to the user.
    The actual method is obtained from the PaymentIntent/Charge when the webhook
    has STRIPE_SECRET_KEY configured. If Stripe lookup is unavailable, return an
    empty string rather than writing the misleading offered-method list.
    """
    secret_key = str(os.environ.get("STRIPE_SECRET_KEY", "") or "").strip()
    payment_intent_id = str(session.get("payment_intent", "") or "").strip()
    if not secret_key or not payment_intent_id:
        return ""

    previous_key = getattr(stripe, "api_key", None)
    stripe.api_key = secret_key
    try:
        intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=["latest_charge"],
        )
        if not isinstance(intent, dict):
            if hasattr(intent, "to_dict_recursive"):
                intent = intent.to_dict_recursive()
            elif hasattr(intent, "to_dict"):
                intent = intent.to_dict()

        charge = (intent or {}).get("latest_charge")
        if isinstance(charge, str) and charge:
            charge = stripe.Charge.retrieve(charge)
        if charge is not None and not isinstance(charge, dict):
            if hasattr(charge, "to_dict_recursive"):
                charge = charge.to_dict_recursive()
            elif hasattr(charge, "to_dict"):
                charge = charge.to_dict()

        details = (charge or {}).get("payment_method_details") or {}
        method = str(details.get("type", "") or "").strip().lower()
        if method:
            return method

        payment_method = (intent or {}).get("payment_method")
        if isinstance(payment_method, dict):
            return str(payment_method.get("type", "") or "").strip().lower()
        if isinstance(payment_method, str) and payment_method:
            pm = stripe.PaymentMethod.retrieve(payment_method)
            if not isinstance(pm, dict):
                if hasattr(pm, "to_dict_recursive"):
                    pm = pm.to_dict_recursive()
                elif hasattr(pm, "to_dict"):
                    pm = pm.to_dict()
            return str((pm or {}).get("type", "") or "").strip().lower()
    except Exception as exc:
        print(
            "Could not resolve actual Stripe payment method; "
            f"leaving PAYMENT_METHOD unchanged: {type(exc).__name__}: {exc}"
        )
    finally:
        stripe.api_key = previous_key

    return ""


def _mark_transaction_failure(
    *,
    store: TransactionSheetStore | None,
    stripe_session_id: str,
    stripe_status: str,
    failure_reason: str,
) -> tuple[bool, str]:
    if store is None:
        return True, ""

    try:
        store.mark_payment_failed_by_stripe_session(
            stripe_session_id=stripe_session_id,
            failed_at=dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
            stripe_status=stripe_status,
            failure_reason=failure_reason,
        )
        return True, ""
    except TransactionStoreError as exc:
        return False, str(exc)



def _refund_amount_major(refund_obj: dict) -> str:
    try:
        amount = Decimal(str(refund_obj.get("amount", 0) or 0)) / Decimal("100")
        return f"{amount:.2f}"
    except Exception:
        return ""


def _handle_refund_event(*, event: dict, event_type: str, refund_obj: dict):
    """Reconcile Stripe refund events into REFUNDS/EVENT_ENTRIES/OUTPUT.

    Refunds created by this application carry the internal REFUND_ID in Stripe
    metadata. Events without a matching internal refund are acknowledged and
    ignored so unrelated Dashboard activity does not create retry storms.
    """
    google_client = create_google_client(_json_env("GCP_SERVICE_ACCOUNT_JSON"))
    transaction_sheet_url = _transaction_sheet_url()
    if not transaction_sheet_url:
        return jsonify({"error": "TRANSACTION_SHEET_URL/OUTPUT_SHEET_URL is missing"}), 500

    try:
        store = TransactionSheetStore(
            google_client=google_client,
            sheet_url=transaction_sheet_url,
        )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Transaction store unavailable: {exc}"}), 500

    metadata = refund_obj.get("metadata") or {}
    refund_id = str(metadata.get("refund_id", "") or "").strip()
    stripe_refund_id = str(refund_obj.get("id", "") or "").strip()

    refund_row = None
    try:
        if refund_id:
            refund_row = store.find_first("REFUNDS", "REFUND_ID", refund_id)
        if not refund_row and stripe_refund_id:
            refund_row = store.find_first(
                "REFUNDS",
                "STRIPE_REFUND_ID",
                stripe_refund_id,
            )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Refund lookup failed: {exc}"}), 500

    if not refund_row:
        return jsonify(
            {
                "received": True,
                "ignored": "untracked_refund",
                "stripe_refund_id": stripe_refund_id,
            }
        ), 200

    refund_id = str(refund_row.get("REFUND_ID", "") or refund_id).strip()
    entry_id = str(refund_row.get("ENTRY_ID", "") or metadata.get("entry_id", "") or "").strip()
    order_id = str(refund_row.get("ORDER_ID", "") or metadata.get("order_id", "") or "").strip()
    refund_type = str(
        refund_row.get("REFUND_TYPE", "")
        or metadata.get("refund_type", "")
        or "WITHDRAWAL"
    ).strip().upper()
    original_entry_fee = str(
        refund_row.get("ORIGINAL_ENTRY_FEE", "")
        or metadata.get("original_entry_fee", "")
        or ""
    ).strip()
    target_entry_fee = str(
        refund_row.get("TARGET_ENTRY_FEE", "")
        or metadata.get("target_entry_fee", "")
        or ""
    ).strip()
    refund_group_id = str(
        refund_row.get("REFUND_GROUP_ID", "")
        or metadata.get("refund_group_id", "")
        or ""
    ).strip()
    refund_sequence = str(
        refund_row.get("REFUND_SEQUENCE", "")
        or metadata.get("refund_sequence", "")
        or ""
    ).strip()
    refund_group_total = str(
        refund_row.get("REFUND_GROUP_TOTAL", "")
        or metadata.get("refund_group_total", "")
        or ""
    ).strip()
    source_payment_purpose = str(
        refund_row.get("SOURCE_PAYMENT_PURPOSE", "")
        or metadata.get("source_payment_purpose", "")
        or ""
    ).strip()
    is_fee_decrease_refund = refund_type == "FEE_DECREASE"
    is_split_withdrawal = (
        refund_type == "WITHDRAWAL_SPLIT" and bool(refund_group_id)
    )

    stripe_status = str(refund_obj.get("status", "") or "").strip().lower()
    failure_reason = str(refund_obj.get("failure_reason", "") or "").strip()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    if event_type == "refund.failed" or stripe_status in {"failed", "canceled"}:
        internal_status = "REFUND_FAILED"
        entry_payment_status = (
            "REFUND_STARTED" if is_split_withdrawal else "PAYMENT_COMPLETE"
        )
    elif stripe_status == "succeeded":
        internal_status = "REFUND_COMPLETE"
        # A fee decrease leaves the registration active and fully settled at
        # its revised amount. A split withdrawal is complete only when every
        # child allocation in the group has completed.
        entry_payment_status = (
            "PAYMENT_COMPLETE"
            if is_fee_decrease_refund
            else "REFUND_STARTED"
            if is_split_withdrawal
            else "REFUND_COMPLETE"
        )
    else:
        internal_status = "REFUND_STARTED"
        entry_payment_status = "REFUND_STARTED"

    approved_by = str(metadata.get("approved_by_user_id", "") or "").strip()
    approved_at = str(metadata.get("approved_at", "") or "").strip()
    approved_amount = _refund_amount_major(refund_obj)

    before = dict(refund_row)
    updates = {
        "STATUS": internal_status,
        "REFUND_TYPE": refund_type,
        "ORIGINAL_ENTRY_FEE": original_entry_fee,
        "TARGET_ENTRY_FEE": target_entry_fee,
        "REFUND_GROUP_ID": refund_group_id,
        "REFUND_SEQUENCE": refund_sequence,
        "REFUND_GROUP_TOTAL": refund_group_total,
        "SOURCE_PAYMENT_PURPOSE": source_payment_purpose,
        "STRIPE_REFUND_ID": stripe_refund_id,
        "STRIPE_STATUS": stripe_status,
        "FAILURE_REASON": failure_reason,
        "UPDATED_AT": now,
    }
    if approved_amount:
        updates["APPROVED_AMOUNT"] = approved_amount
    if approved_by:
        updates["APPROVED_BY_USER_ID"] = approved_by
        updates["DECIDED_BY_USER_ID"] = approved_by
    if approved_at:
        updates["APPROVED_AT"] = approved_at
        updates["DECIDED_AT"] = approved_at
    if internal_status == "REFUND_COMPLETE":
        updates["COMPLETED_AT"] = now

    try:
        store.update_by_id("REFUNDS", refund_id, updates)

        group_completed_now = False
        group_rows: list[dict[str, str]] = []
        if is_split_withdrawal:
            group_rows = [
                row
                for row in store.list_rows("REFUNDS")
                if str(row.get("REFUND_GROUP_ID", "") or "").strip()
                == refund_group_id
            ]
            if not group_rows:
                raise TransactionStoreError(
                    f"Refund group {refund_group_id} could not be reloaded."
                )
            group_completed_now = all(
                str(row.get("STATUS", "") or "").strip().upper()
                == "REFUND_COMPLETE"
                for row in group_rows
            )
            entry_payment_status = (
                "REFUND_COMPLETE"
                if group_completed_now
                else "REFUND_STARTED"
            )

        fee_decrease_applied_now = False
        entry_fee_before = ""

        if entry_id:
            entry_updates = {
                "PAYMENT_STATUS": entry_payment_status,
                "PAYMENT_STATUS_CHANGED_AT": now,
                "UPDATED_AT": now,
            }
            if internal_status == "REFUND_COMPLETE" and is_fee_decrease_refund:
                if not target_entry_fee:
                    raise TransactionStoreError(
                        f"Fee-decrease refund {refund_id} has no TARGET_ENTRY_FEE."
                    )

                # Stripe can deliver both refund.updated and refund.created for
                # the same successful refund. Read the current entry immediately
                # before applying the target fee and emit the fee-change audit
                # only when this delivery actually changes the stored fee. The
                # state update itself remains safely idempotent.
                current_entry = store.find_first(
                    "EVENT_ENTRIES",
                    "ENTRY_ID",
                    entry_id,
                )
                if current_entry is None:
                    raise TransactionStoreError(
                        f"Could not find EVENT_ENTRIES row for {entry_id}."
                    )

                entry_fee_before = str(
                    current_entry.get("ENTRY_FEE", "") or ""
                ).strip()
                try:
                    fee_decrease_applied_now = (
                        Decimal(entry_fee_before or "0")
                        != Decimal(target_entry_fee)
                    )
                except Exception:
                    fee_decrease_applied_now = (
                        entry_fee_before != str(target_entry_fee).strip()
                    )

                entry_updates["ENTRY_FEE"] = target_entry_fee

            store.update_by_id(
                "EVENT_ENTRIES",
                entry_id,
                entry_updates,
            )

        if (
            internal_status == "REFUND_COMPLETE"
            and is_fee_decrease_refund
            and entry_id
            and fee_decrease_applied_now
        ):
            # Only the delivery that actually changes ENTRY_FEE emits this
            # business-level audit. Event-level STRIPE_REFUND_RECONCILED rows
            # remain separate for refund.created/refund.updated traceability.
            store.append_audit_log(
                {
                    "AUDIT_ID": f"AUD-FEE-{refund_id}",
                    "TIMESTAMP": now,
                    "USER_ID": "SYSTEM_STRIPE",
                    "USER_EMAIL": "",
                    "ACTION": "ENTRY_FEE_DECREASE_APPLIED",
                    "ENTITY_TYPE": "EVENT_ENTRY",
                    "ENTITY_ID": entry_id,
                    "ORDER_ID": order_id,
                    "BEFORE_JSON": json.dumps(
                        {"ENTRY_FEE": entry_fee_before},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "AFTER_JSON": json.dumps(
                        {"ENTRY_FEE": target_entry_fee},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "REASON": f"Stripe refund {stripe_refund_id} completed",
                }
            )

        if is_split_withdrawal and group_completed_now:
            # Stripe can deliver refund.created/refund.updated close together, and
            # separate child refunds in the same group can also complete nearly
            # concurrently. Elect exactly one completed child as the business-level
            # completion-audit leader using the latest persisted completion/update
            # timestamp (then sequence/id as deterministic tie-breakers). This avoids
            # the duplicate business audit observed earlier with fee-decrease webhooks.
            completion_leader = max(
                group_rows,
                key=lambda row: (
                    str(
                        row.get("COMPLETED_AT", "")
                        or row.get("UPDATED_AT", "")
                        or ""
                    ).strip(),
                    str(row.get("REFUND_SEQUENCE", "") or "").strip(),
                    str(row.get("REFUND_ID", "") or "").strip(),
                ),
            )
            completion_leader_id = str(
                completion_leader.get("REFUND_ID", "") or ""
            ).strip()

            if refund_id == completion_leader_id:
                try:
                    computed_group_total = sum(
                        (
                            Decimal(
                                str(row.get("APPROVED_AMOUNT", "") or "0")
                            )
                            for row in group_rows
                        ),
                        Decimal("0"),
                    )
                    group_total_value = (
                        refund_group_total
                        or f"{computed_group_total:.2f}"
                    )
                except Exception:
                    group_total_value = refund_group_total

                store.append_audit_log(
                    {
                        "AUDIT_ID": f"AUD-RFG-COMPLETE-{refund_group_id}",
                        "TIMESTAMP": now,
                        "USER_ID": "SYSTEM_STRIPE",
                        "USER_EMAIL": "",
                        "ACTION": "MULTI_PAYMENT_WITHDRAWAL_REFUND_COMPLETED",
                        "ENTITY_TYPE": "REFUND_GROUP",
                        "ENTITY_ID": refund_group_id,
                        "ORDER_ID": order_id,
                        "BEFORE_JSON": "{}",
                        "AFTER_JSON": json.dumps(
                            {
                                "REFUND_GROUP_ID": refund_group_id,
                                "TOTAL": group_total_value,
                                "ALLOCATIONS": [
                                    {
                                        "REFUND_ID": str(
                                            row.get("REFUND_ID", "") or ""
                                        ).strip(),
                                        "PAYMENT_ID": str(
                                            row.get("PAYMENT_ID", "") or ""
                                        ).strip(),
                                        "AMOUNT": str(
                                            row.get("APPROVED_AMOUNT", "") or ""
                                        ).strip(),
                                        "STRIPE_REFUND_ID": str(
                                            row.get("STRIPE_REFUND_ID", "") or ""
                                        ).strip(),
                                    }
                                    for row in group_rows
                                ],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "REASON": "All Stripe refund allocations completed",
                    }
                )

        after = dict(before)
        after.update(updates)
        event_id = str(event.get("id", "") or "").strip()
        audit_id = f"AUD-{event_id}" if event_id else f"AUD-STRIPE-{stripe_refund_id}-{stripe_status}"
        store.append_audit_log(
            {
                "AUDIT_ID": audit_id,
                "TIMESTAMP": now,
                "USER_ID": "SYSTEM_STRIPE",
                "USER_EMAIL": "",
                "ACTION": "STRIPE_REFUND_RECONCILED",
                "ENTITY_TYPE": "REFUND",
                "ENTITY_ID": refund_id,
                "ORDER_ID": order_id,
                "BEFORE_JSON": json.dumps(before, ensure_ascii=False, sort_keys=True),
                "AFTER_JSON": json.dumps(after, ensure_ascii=False, sort_keys=True),
                "REASON": event_type,
            }
        )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Refund reconciliation failed: {exc}"}), 500

    output_url = str(os.environ.get("OUTPUT_SHEET_URL", "") or "").strip()
    if output_url and entry_id:
        try:
            if (
                internal_status == "REFUND_COMPLETE"
                and is_fee_decrease_refund
                and target_entry_fee
            ):
                update_entry_fee_and_payment_status(
                    gc=google_client,
                    output_sheet_url_or_id=output_url,
                    output_worksheet=os.environ.get("OUTPUT_WORKSHEET", ""),
                    entry_id=entry_id,
                    entry_fee=target_entry_fee,
                    payment_status=entry_payment_status,
                )
            else:
                update_entry_payment_status(
                    gc=google_client,
                    output_sheet_url_or_id=output_url,
                    output_worksheet=os.environ.get("OUTPUT_WORKSHEET", ""),
                    entry_id=entry_id,
                    payment_status=entry_payment_status,
                )
        except Exception as exc:
            return jsonify(
                {"error": f"Refund OUTPUT reconciliation failed: {type(exc).__name__}: {exc}"}
            ), 500

    return jsonify(
        {
            "received": True,
            "refund_id": refund_id,
            "stripe_refund_id": stripe_refund_id,
            "status": internal_status,
        }
    ), 200


def _handle_fee_increase_checkout_event(*, event: dict, event_type: str, session: dict):
    """Reconcile one additional Stripe payment for an admin fee increase.

    This path deliberately bypasses PendingPayments and the normal order-level
    completion helper. A top-up must never reconfirm withdrawn sibling entries
    or overwrite the original payment. Only the new PAYMENTS row and its target
    EVENT_ENTRY are changed.
    """
    google_client = create_google_client(_json_env("GCP_SERVICE_ACCOUNT_JSON"))
    transaction_sheet_url = _transaction_sheet_url()
    if not transaction_sheet_url:
        return jsonify({"error": "TRANSACTION_SHEET_URL/OUTPUT_SHEET_URL is missing"}), 500

    try:
        store = TransactionSheetStore(
            google_client=google_client,
            sheet_url=transaction_sheet_url,
        )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Transaction store unavailable: {exc}"}), 500

    metadata = session.get("metadata") or {}
    payment_id = str(metadata.get("payment_id", "") or "").strip()
    actual_session_id = str(session.get("id", "") or "").strip()

    try:
        payment = (
            store.find_first("PAYMENTS", "PAYMENT_ID", payment_id)
            if payment_id
            else None
        )
        if payment is None and actual_session_id:
            payment = store.find_first(
                "PAYMENTS",
                "STRIPE_CHECKOUT_SESSION_ID",
                actual_session_id,
            )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Fee-increase payment lookup failed: {exc}"}), 500

    if not payment:
        return jsonify(
            {
                "error": "Tracked fee-increase payment was not found",
                "stripe_session_id": actual_session_id,
            }
        ), 404

    purpose = str(payment.get("PAYMENT_PURPOSE", "") or metadata.get("payment_purpose", "") or "").strip().upper()
    if purpose != "FEE_INCREASE":
        return jsonify({"error": "PAYMENTS row is not a FEE_INCREASE payment"}), 400

    payment_id = str(payment.get("PAYMENT_ID", "") or payment_id).strip()
    order_id = str(payment.get("ORDER_ID", "") or metadata.get("order_id", "") or "").strip()
    entry_id = str(payment.get("ENTRY_ID", "") or metadata.get("entry_id", "") or "").strip()
    expected_session_id = str(payment.get("STRIPE_CHECKOUT_SESSION_ID", "") or "").strip()
    expected_currency = str(payment.get("CURRENCY", "") or session.get("currency", "") or "SGD").strip().lower()
    target_entry_fee = str(payment.get("TARGET_ENTRY_FEE", "") or metadata.get("target_entry_fee", "") or "").strip()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    failure_events = {
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }

    if event_type in failure_events:
        if expected_session_id and actual_session_id and expected_session_id != actual_session_id:
            return jsonify(
                {
                    "received": True,
                    "ignored": "stale_fee_increase_checkout_failure",
                    "session_id": actual_session_id,
                }
            ), 200

        failure_status = "expired" if event_type == "checkout.session.expired" else "failed"
        try:
            store.update_by_id(
                "PAYMENTS",
                payment_id,
                {
                    "DISPLAY_STATUS": "REQUIRED",
                    "STRIPE_STATUS": failure_status,
                    "LAST_ATTEMPT_AT": now,
                    "FAILURE_REASON": event_type,
                    "STRIPE_CHECKOUT_URL": "",
                },
            )
            if entry_id:
                store.update_by_id(
                    "EVENT_ENTRIES",
                    entry_id,
                    {
                        "PAYMENT_STATUS": "REQUIRED",
                        "PAYMENT_STATUS_CHANGED_AT": now,
                        "UPDATED_AT": now,
                    },
                )
            event_id = str(event.get("id", "") or "").strip()
            store.append_audit_log(
                {
                    "AUDIT_ID": f"AUD-{event_id}" if event_id else f"AUD-FEEINC-FAIL-{payment_id}-{failure_status}",
                    "TIMESTAMP": now,
                    "USER_ID": "SYSTEM_STRIPE",
                    "USER_EMAIL": "",
                    "ACTION": "STRIPE_FEE_INCREASE_RECONCILED",
                    "ENTITY_TYPE": "PAYMENT",
                    "ENTITY_ID": payment_id,
                    "ORDER_ID": order_id,
                    "BEFORE_JSON": json.dumps(payment, ensure_ascii=False, sort_keys=True),
                    "AFTER_JSON": json.dumps(
                        {
                            **payment,
                            "DISPLAY_STATUS": "REQUIRED",
                            "STRIPE_STATUS": failure_status,
                            "FAILURE_REASON": event_type,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "REASON": event_type,
                }
            )
        except TransactionStoreError as exc:
            return jsonify({"error": f"Fee-increase failure reconciliation failed: {exc}"}), 500

        output_url = str(os.environ.get("OUTPUT_SHEET_URL", "") or "").strip()
        if output_url and entry_id:
            try:
                update_entry_payment_status(
                    gc=google_client,
                    output_sheet_url_or_id=output_url,
                    output_worksheet=os.environ.get("OUTPUT_WORKSHEET", ""),
                    entry_id=entry_id,
                    payment_status="REQUIRED",
                )
            except Exception as exc:
                return jsonify(
                    {"error": f"Fee-increase failure OUTPUT reconciliation failed: {type(exc).__name__}: {exc}"}
                ), 500
        return jsonify({"received": True, "status": "REQUIRED", "payment_id": payment_id}), 200

    # Card is normally paid on checkout.session.completed. PayNow can complete
    # Checkout before funds settle; in that case wait for async_payment_succeeded.
    if str(session.get("payment_status", "") or "").lower() != "paid":
        return jsonify({"received": True, "paid": False, "payment_id": payment_id}), 200

    if expected_session_id and actual_session_id != expected_session_id:
        return jsonify(
            {
                "error": "Fee-increase Stripe session mismatch",
                "expected": expected_session_id,
                "received": actual_session_id,
            }
        ), 400

    try:
        expected_amount = Decimal(str(payment.get("AMOUNT", "0") or "0"))
        actual_amount = Decimal(str(session.get("amount_total", 0) or 0)) / Decimal("100")
    except Exception as exc:
        return jsonify({"error": f"Could not validate additional payment amount: {exc}"}), 500

    actual_currency = str(session.get("currency", "") or "").strip().lower()
    if actual_amount != expected_amount or actual_currency != expected_currency:
        mismatch = (
            f"Fee-increase payment mismatch: expected {expected_currency.upper()} {expected_amount:.2f}, "
            f"received {actual_currency.upper()} {actual_amount:.2f}"
        )
        try:
            store.update_by_id(
                "PAYMENTS",
                payment_id,
                {
                    "DISPLAY_STATUS": "REQUIRED",
                    "STRIPE_STATUS": "validation_mismatch",
                    "LAST_ATTEMPT_AT": now,
                    "FAILURE_REASON": mismatch,
                },
            )
            if entry_id:
                store.update_by_id(
                    "EVENT_ENTRIES",
                    entry_id,
                    {
                        "PAYMENT_STATUS": "REQUIRED",
                        "PAYMENT_STATUS_CHANGED_AT": now,
                        "UPDATED_AT": now,
                    },
                )
        except TransactionStoreError as exc:
            return jsonify({"error": f"Additional-payment mismatch update failed: {exc}"}), 500
        output_url = str(os.environ.get("OUTPUT_SHEET_URL", "") or "").strip()
        if output_url and entry_id:
            try:
                update_entry_payment_status(
                    gc=google_client,
                    output_sheet_url_or_id=output_url,
                    output_worksheet=os.environ.get("OUTPUT_WORKSHEET", ""),
                    entry_id=entry_id,
                    payment_status="REQUIRED",
                )
            except Exception as exc:
                return jsonify({"error": f"Additional-payment mismatch OUTPUT update failed: {type(exc).__name__}: {exc}"}), 500
        return jsonify({"error": mismatch}), 400

    if not entry_id or not target_entry_fee:
        return jsonify({"error": "Fee-increase payment is missing ENTRY_ID or TARGET_ENTRY_FEE"}), 500

    try:
        entry_before = store.find_first("EVENT_ENTRIES", "ENTRY_ID", entry_id)
        if entry_before is None:
            raise TransactionStoreError(f"Could not find EVENT_ENTRIES row for {entry_id}.")

        entry_fee_before = str(entry_before.get("ENTRY_FEE", "") or "").strip()
        try:
            fee_changed = Decimal(entry_fee_before or "0") != Decimal(target_entry_fee)
        except Exception:
            fee_changed = entry_fee_before != target_entry_fee

        payment_intent_id = str(session.get("payment_intent", "") or "").strip()
        actual_payment_method = _actual_payment_method(session)
        store.update_by_id(
            "PAYMENTS",
            payment_id,
            {
                "STRIPE_PAYMENT_INTENT_ID": payment_intent_id,
                "PAYMENT_METHOD": actual_payment_method,
                "DISPLAY_STATUS": "PAYMENT_COMPLETE",
                "STRIPE_STATUS": "paid",
                "PAID_AT": now,
                "LAST_ATTEMPT_AT": now,
                "FAILURE_REASON": "",
            },
        )

        store.update_by_id(
            "EVENT_ENTRIES",
            entry_id,
            {
                "ENTRY_FEE": target_entry_fee,
                "PAYMENT_STATUS": "PAYMENT_COMPLETE",
                "PAYMENT_STATUS_CHANGED_AT": now,
                "UPDATED_AT": now,
            },
        )

        if fee_changed:
            store.append_audit_log(
                {
                    "AUDIT_ID": f"AUD-FEEINC-{payment_id}",
                    "TIMESTAMP": now,
                    "USER_ID": "SYSTEM_STRIPE",
                    "USER_EMAIL": "",
                    "ACTION": "ENTRY_FEE_INCREASE_APPLIED",
                    "ENTITY_TYPE": "EVENT_ENTRY",
                    "ENTITY_ID": entry_id,
                    "ORDER_ID": order_id,
                    "BEFORE_JSON": json.dumps({"ENTRY_FEE": entry_fee_before}, ensure_ascii=False, sort_keys=True),
                    "AFTER_JSON": json.dumps({"ENTRY_FEE": target_entry_fee}, ensure_ascii=False, sort_keys=True),
                    "REASON": f"Additional Stripe payment {payment_id} completed",
                }
            )

        event_id = str(event.get("id", "") or "").strip()
        payment_after = dict(payment)
        payment_after.update(
            {
                "STRIPE_PAYMENT_INTENT_ID": payment_intent_id,
                "PAYMENT_METHOD": actual_payment_method,
                "DISPLAY_STATUS": "PAYMENT_COMPLETE",
                "STRIPE_STATUS": "paid",
                "PAID_AT": now,
                "LAST_ATTEMPT_AT": now,
                "FAILURE_REASON": "",
            }
        )
        store.append_audit_log(
            {
                "AUDIT_ID": f"AUD-{event_id}" if event_id else f"AUD-FEEINC-PAID-{payment_id}",
                "TIMESTAMP": now,
                "USER_ID": "SYSTEM_STRIPE",
                "USER_EMAIL": "",
                "ACTION": "STRIPE_FEE_INCREASE_RECONCILED",
                "ENTITY_TYPE": "PAYMENT",
                "ENTITY_ID": payment_id,
                "ORDER_ID": order_id,
                "BEFORE_JSON": json.dumps(payment, ensure_ascii=False, sort_keys=True),
                "AFTER_JSON": json.dumps(payment_after, ensure_ascii=False, sort_keys=True),
                "REASON": event_type,
            }
        )
    except TransactionStoreError as exc:
        return jsonify({"error": f"Fee-increase payment reconciliation failed: {exc}"}), 500

    output_url = str(os.environ.get("OUTPUT_SHEET_URL", "") or "").strip()
    if output_url:
        try:
            update_entry_fee_and_payment_status(
                gc=google_client,
                output_sheet_url_or_id=output_url,
                output_worksheet=os.environ.get("OUTPUT_WORKSHEET", ""),
                entry_id=entry_id,
                entry_fee=target_entry_fee,
                payment_status="PAYMENT_COMPLETE",
            )
        except Exception as exc:
            return jsonify(
                {"error": f"Fee-increase OUTPUT reconciliation failed: {type(exc).__name__}: {exc}"}
            ), 500

    return jsonify(
        {
            "received": True,
            "payment_id": payment_id,
            "entry_id": entry_id,
            "status": "PAYMENT_COMPLETE",
        }
    ), 200


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

    # Recent stripe-python versions return Stripe Event/StripeObject
    # instances rather than plain dictionaries. Convert them before
    # using dict-style access such as .get().
    if not isinstance(event, dict):
        if hasattr(event, "to_dict_recursive"):
            event = event.to_dict_recursive()
        elif hasattr(event, "to_dict"):
            event = event.to_dict()

    event_type = str(event.get("type", "") or "")
    session = event["data"]["object"]

    if not isinstance(session, dict):
        if hasattr(session, "to_dict_recursive"):
            session = session.to_dict_recursive()
        elif hasattr(session, "to_dict"):
            session = session.to_dict()

    checkout_events = {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }
    session_metadata = session.get("metadata") or {}
    if (
        event_type in checkout_events
        and str(session_metadata.get("payment_purpose", "") or "").strip().upper() == "FEE_INCREASE"
    ):
        return _handle_fee_increase_checkout_event(
            event=event,
            event_type=event_type,
            session=session,
        )

    refund_events = {"refund.created", "refund.updated", "refund.failed"}
    if event_type in refund_events:
        return _handle_refund_event(
            event=event,
            event_type=event_type,
            refund_obj=session,
        )

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

    reference_id = str(
        session.get("client_reference_id")
        or session.get("metadata", {}).get("registration_id")
        or ""
    ).strip()

    if not reference_id:
        return jsonify(
            {"error": "Missing registration/order ID"}
        ), 400

    # New Phase 2B orders use the ORDER_ID as Stripe's client_reference_id.
    # Older pending rows may still have a legacy registration reference.
    order_id = reference_id
    is_transaction_order = order_id.upper().startswith("ORD-")

    google_client = create_google_client(
        _json_env("GCP_SERVICE_ACCOUNT_JSON")
    )

    transaction_store = None
    if is_transaction_order:
        transaction_sheet_url = _transaction_sheet_url()
        if not transaction_sheet_url:
            return jsonify(
                {
                    "error":
                    "TRANSACTION_SHEET_URL/OUTPUT_SHEET_URL is missing"
                }
            ), 500

        try:
            transaction_store = TransactionSheetStore(
                google_client=google_client,
                sheet_url=transaction_sheet_url,
            )
        except TransactionStoreError as exc:
            return jsonify(
                {"error": f"Transaction store unavailable: {exc}"}
            ), 500

    pending_worksheet = get_pending_worksheet(
        google_client,
        os.environ["PENDING_PAYMENT_SHEET_URL"],
        os.environ.get(
            "PENDING_PAYMENT_WORKSHEET",
            "PendingPayments",
        ),
    )

    actual_session_id = str(session.get("id", "") or "").strip()

    row_number, pending = _find_pending_for_session(
        pending_worksheet,
        reference_id,
        actual_session_id,
    )

    if not pending or not row_number:
        return jsonify(
            {"error": "Pending registration/order not found"}
        ), 404

    expected_session_id = str(
        pending.get("stripe_checkout_session_id", "") or ""
    ).strip()

    # ------------------------------------------------------------------
    # Explicit Stripe failure / expiry
    # ------------------------------------------------------------------
    if event_type in handled_failure_events:
        # If the user has already been issued a replacement Checkout session,
        # a delayed expiry/failure event from an older attempt must not downgrade
        # the current order or trigger endless Stripe retries.
        if (
            expected_session_id
            and actual_session_id
            and actual_session_id != expected_session_id
        ):
            return jsonify(
                {
                    "received": True,
                    "ignored": "stale_checkout_failure",
                    "session_id": actual_session_id,
                }
            ), 200

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

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status=failure_status.lower(),
            failure_reason=event_type,
        )
        if not ok:
            # 500 causes Stripe to retry. All transaction-store updates are
            # idempotent, so retries are safe.
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify(
            {"received": True, "status": failure_status}
        ), 200

    # For card payments this is normally paid at checkout.session.completed.
    # If the method is asynchronous, wait for async_payment_succeeded.
    if str(session.get("payment_status", "")).lower() != "paid":
        return jsonify(
            {"received": True, "paid": False}
        ), 200

    if (
        expected_session_id
        and actual_session_id != expected_session_id
    ):
        mismatch_reason = (
            "Stripe session mismatch: expected "
            f"{expected_session_id}, received {actual_session_id}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=mismatch_reason,
        )

        # The transaction row contains the expected session ID, not the
        # mismatched incoming one.
        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=expected_session_id,
            stripe_status="session_mismatch",
            failure_reason=mismatch_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

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
        amount_reason = (
            f"Amount mismatch: expected {expected_amount}, "
            f"received {actual_amount}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=amount_reason,
        )

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status="amount_mismatch",
            failure_reason=amount_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

        return jsonify({"error": "Amount mismatch"}), 400

    if actual_currency != expected_currency:
        currency_reason = (
            f"Currency mismatch: expected {expected_currency}, "
            f"received {actual_currency}"
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="FAILED",
            error=currency_reason,
        )

        ok, error = _mark_transaction_failure(
            store=transaction_store,
            stripe_session_id=actual_session_id,
            stripe_status="currency_mismatch",
            failure_reason=currency_reason,
        )
        if not ok:
            return jsonify(
                {"error": f"Transaction failure update failed: {error}"}
            ), 500

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

    confirmed_at = dt.datetime.now(
        dt.timezone.utc
    ).isoformat()

    # ------------------------------------------------------------------
    # Authoritative transaction-table confirmation
    # ------------------------------------------------------------------
    # Run this even if PendingPayments already says PAID. If a previous
    # webhook invocation stopped after the legacy pending update, a Stripe
    # retry will repair the transaction tables.
    if transaction_store is not None:
        try:
            transaction_store.mark_payment_complete_by_stripe_session(
                stripe_session_id=actual_session_id,
                stripe_payment_intent_id=payment_intent_id,
                paid_at=confirmed_at,
                payment_method=_actual_payment_method(session),
                processing_fee="",
            )
        except TransactionStoreError as exc:
            return jsonify(
                {"error": f"Transaction completion failed: {exc}"}
            ), 500

    # ------------------------------------------------------------------
    # Existing legacy OUTPUT projection + PendingPayments state
    # ------------------------------------------------------------------
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
            order_id=order_id,
            entry_rows=entry_rows,
            stripe_session_id=actual_session_id,
            stripe_payment_intent_id=payment_intent_id,
        )

        update_pending_fields(
            pending_worksheet,
            row_number,
            status="PAID",
            stripe_payment_intent_id=payment_intent_id,
            confirmed_at=confirmed_at,
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
            registration_id=reference_id,
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
            "order_id": order_id if is_transaction_order else "",
            "registration_id": reference_id,
            "status": "PAID",
        }
    ), 200
