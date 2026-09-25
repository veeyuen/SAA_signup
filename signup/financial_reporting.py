from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd


COMPLETED_PAYMENT_STATUS = "PAYMENT_COMPLETE"
COMPLETED_REFUND_STATUS = "REFUND_COMPLETE"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _d(value: Any, default: str = "0") -> Decimal:
    raw = _clean(value).replace(",", "")
    if raw == "":
        raw = default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _money_float(value: Decimal | None):
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def _is_complete_payment(row: dict) -> bool:
    return _clean(row.get("DISPLAY_STATUS")).upper() == COMPLETED_PAYMENT_STATUS


def _is_complete_refund(row: dict) -> bool:
    return _clean(row.get("STATUS")).upper() == COMPLETED_REFUND_STATUS


def _known_money(row: dict, key: str) -> bool:
    return _clean(row.get(key)) != ""


def _competition_year(entry: dict, competition_year_by_id: dict[str, int]) -> int | None:
    competition_id = _clean(entry.get("COMPETITION_ID"))
    year = competition_year_by_id.get(competition_id)
    if year:
        return int(year)
    return None


def _age_from_dob(dob: Any, year: int | None) -> int | None:
    if not year:
        return None
    raw = _clean(dob)
    if not raw:
        return None
    try:
        dob_ts = pd.to_datetime(raw, errors="coerce")
        if pd.isna(dob_ts):
            return None
        return int(year) - int(dob_ts.year)
    except Exception:
        return None


def reconstructed_original_entry_fees(
    *,
    entries: list[dict],
    payments: list[dict],
    refunds: list[dict],
) -> dict[str, Decimal]:
    """Reconstruct each entry's fee when its original order payment was taken.

    ENTRY_FEE is intentionally mutable after an approved fee amendment. The
    immutable fee-adjustment ledgers let us reverse those changes for reporting:

        original fee = current fee - completed top-ups + completed fee-decrease refunds

    Withdrawal refunds do not alter ENTRY_FEE and therefore are not part of this
    reconstruction.
    """
    topups_by_entry: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for payment in payments:
        if not _is_complete_payment(payment):
            continue
        if _clean(payment.get("PAYMENT_PURPOSE")).upper() != "FEE_INCREASE":
            continue
        entry_id = _clean(payment.get("ENTRY_ID"))
        if entry_id:
            topups_by_entry[entry_id] += _d(payment.get("AMOUNT"))

    fee_decrease_by_entry: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for refund in refunds:
        if not _is_complete_refund(refund):
            continue
        if _clean(refund.get("REFUND_TYPE")).upper() != "FEE_DECREASE":
            continue
        entry_id = _clean(refund.get("ENTRY_ID"))
        if entry_id:
            fee_decrease_by_entry[entry_id] += _d(refund.get("APPROVED_AMOUNT"))

    out: dict[str, Decimal] = {}
    for entry in entries:
        entry_id = _clean(entry.get("ENTRY_ID"))
        if not entry_id:
            continue
        current_fee = _d(entry.get("ENTRY_FEE"))
        original = (
            current_fee
            - topups_by_entry[entry_id]
            + fee_decrease_by_entry[entry_id]
        )
        out[entry_id] = max(Decimal("0"), original)
    return out


def _allocate_original_payment_fees(
    *,
    entries: list[dict],
    payments: list[dict],
    original_fee_by_entry: dict[str, Decimal],
) -> tuple[dict[str, Decimal], set[str]]:
    """Allocate original payment Stripe fees to entries pro-rata by original fee.

    Returns (fee_by_entry, unknown_fee_entry_ids). Additional FEE_INCREASE
    payments are deliberately excluded here because they map directly to ENTRY_ID.
    """
    entries_by_payment: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        payment_id = _clean(entry.get("PAYMENT_ID"))
        if payment_id:
            entries_by_payment[payment_id].append(entry)

    fee_by_entry: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    unknown: set[str] = set()

    for payment in payments:
        if not _is_complete_payment(payment):
            continue
        if _clean(payment.get("PAYMENT_PURPOSE")).upper() == "FEE_INCREASE":
            continue
        if _clean(payment.get("PROVIDER")).upper() not in {"", "STRIPE"}:
            continue

        payment_id = _clean(payment.get("PAYMENT_ID"))
        payment_entries = entries_by_payment.get(payment_id, [])
        if not payment_entries:
            continue

        if not _known_money(payment, "STRIPE_FEE_ACTUAL"):
            unknown.update(_clean(e.get("ENTRY_ID")) for e in payment_entries)
            continue

        fee = _d(payment.get("STRIPE_FEE_ACTUAL"))
        weights = [
            original_fee_by_entry.get(_clean(e.get("ENTRY_ID")), Decimal("0"))
            for e in payment_entries
        ]
        total_weight = sum(weights, Decimal("0"))
        if total_weight <= 0:
            # Equal allocation is preferable to dropping a known fee when old
            # legacy rows do not contain an entry-fee basis.
            share = fee / Decimal(len(payment_entries))
            for entry in payment_entries:
                fee_by_entry[_clean(entry.get("ENTRY_ID"))] += share
            continue

        for entry, weight in zip(payment_entries, weights):
            if weight <= 0:
                continue
            fee_by_entry[_clean(entry.get("ENTRY_ID"))] += fee * weight / total_weight

    return dict(fee_by_entry), unknown


def build_entry_financial_report(
    *,
    entries: list[dict],
    payments: list[dict],
    refunds: list[dict],
    competition_year_by_id: dict[str, int] | None = None,
) -> pd.DataFrame:
    competition_year_by_id = competition_year_by_id or {}
    payment_by_id = {
        _clean(p.get("PAYMENT_ID")): p
        for p in payments
        if _clean(p.get("PAYMENT_ID"))
    }

    original_fee_by_entry = reconstructed_original_entry_fees(
        entries=entries,
        payments=payments,
        refunds=refunds,
    )
    original_fee_alloc, unknown_original_fee_entries = _allocate_original_payment_fees(
        entries=entries,
        payments=payments,
        original_fee_by_entry=original_fee_by_entry,
    )

    completed_topups_by_entry: dict[str, list[dict]] = defaultdict(list)
    for payment in payments:
        if (
            _is_complete_payment(payment)
            and _clean(payment.get("PAYMENT_PURPOSE")).upper() == "FEE_INCREASE"
            and _clean(payment.get("ENTRY_ID"))
        ):
            completed_topups_by_entry[_clean(payment.get("ENTRY_ID"))].append(payment)

    completed_refunds_by_entry: dict[str, list[dict]] = defaultdict(list)
    for refund in refunds:
        if _is_complete_refund(refund) and _clean(refund.get("ENTRY_ID")):
            completed_refunds_by_entry[_clean(refund.get("ENTRY_ID"))].append(refund)

    rows: list[dict] = []
    for entry in entries:
        entry_id = _clean(entry.get("ENTRY_ID"))
        order_id = _clean(entry.get("ORDER_ID"))
        original_payment_id = _clean(entry.get("PAYMENT_ID"))
        original_payment = payment_by_id.get(original_payment_id, {})
        original_fee = original_fee_by_entry.get(entry_id, _d(entry.get("ENTRY_FEE")))

        original_paid = (
            original_fee
            if original_payment and _is_complete_payment(original_payment)
            else Decimal("0")
        )
        topups = completed_topups_by_entry.get(entry_id, [])
        topup_paid = sum((_d(p.get("AMOUNT")) for p in topups), Decimal("0"))
        amount_paid = original_paid + topup_paid

        entry_refunds = completed_refunds_by_entry.get(entry_id, [])
        refund_total = sum(
            (_d(r.get("APPROVED_AMOUNT")) for r in entry_refunds),
            Decimal("0"),
        )
        collected = amount_paid - refund_total

        stripe_fee = original_fee_alloc.get(entry_id, Decimal("0"))
        stripe_fee_known = entry_id not in unknown_original_fee_entries

        for payment in topups:
            if _clean(payment.get("PROVIDER")).upper() in {"", "STRIPE"}:
                if _known_money(payment, "STRIPE_FEE_ACTUAL"):
                    stripe_fee += _d(payment.get("STRIPE_FEE_ACTUAL"))
                else:
                    stripe_fee_known = False

        for refund in entry_refunds:
            # Completed Stripe refunds should have their balance-transaction fee
            # backfilled. A zero is meaningful; blank means not yet reconciled.
            if _clean(refund.get("STRIPE_REFUND_ID")):
                if _known_money(refund, "STRIPE_FEE_ACTUAL"):
                    stripe_fee += _d(refund.get("STRIPE_FEE_ACTUAL"))
                else:
                    stripe_fee_known = False

        current_gross = _d(entry.get("ENTRY_FEE"))
        net_revenue = collected - stripe_fee if stripe_fee_known else None
        comp_year = _competition_year(entry, competition_year_by_id)

        rows.append(
            {
                "TEAM": _clean(entry.get("TEAM_NAME")),
                "ATHLETE_NAME": _clean(entry.get("ATHLETE_NAME")),
                "COUNTRY": _clean(entry.get("NATIONALITY")),
                "GENDER": _clean(entry.get("GENDER")),
                "AGE": _age_from_dob(entry.get("DOB"), comp_year),
                "DIVISION": _clean(entry.get("DIVISION")),
                "EVENT": _clean(entry.get("EVENT_NAME")),
                "GROSS": _money_float(current_gross),
                "AMOUNT_PAID": _money_float(amount_paid),
                "AMOUNT_INVOICED": 0.0,
                "COLLECTED": _money_float(collected),
                "REFUNDS": _money_float(refund_total),
                "STRIPE_FEES": _money_float(stripe_fee) if stripe_fee_known else None,
                "REVENUE": _money_float(net_revenue),
                "COMPETITION_ID": _clean(entry.get("COMPETITION_ID")),
                "ORDER_ID": order_id,
                "REGISTRATION_ID": _clean(entry.get("REGISTRATION_ID")),
                "ENTRY_ID": entry_id,
                "ENTRY_STATUS": _clean(entry.get("STATUS")),
                "PAYMENT_STATUS": _clean(entry.get("PAYMENT_STATUS")),
                "ORIGINAL_ENTRY_FEE": _money_float(original_fee),
                "STRIPE_FINANCIALS_COMPLETE": stripe_fee_known,
            }
        )

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    gender_order = out["GENDER"].map(
        lambda x: 0 if str(x).strip().casefold() == "male" else 1
    )
    out = (
        out.assign(_gender_order=gender_order)
        .sort_values(
            ["TEAM", "_gender_order", "ATHLETE_NAME", "EVENT", "ENTRY_ID"],
            kind="stable",
        )
        .drop(columns=["_gender_order"])
        .reset_index(drop=True)
    )
    return out


def build_transaction_ledger(
    *,
    orders: list[dict],
    payments: list[dict],
    refunds: list[dict],
) -> pd.DataFrame:
    order_map = {
        _clean(o.get("ORDER_ID")): o
        for o in orders
        if _clean(o.get("ORDER_ID"))
    }
    rows: list[dict] = []

    for payment in payments:
        if not _is_complete_payment(payment):
            continue
        order_id = _clean(payment.get("ORDER_ID"))
        order = order_map.get(order_id, {})
        rows.append(
            {
                "DATE": _clean(payment.get("PAID_AT")) or _clean(payment.get("CREATED_AT")),
                "TYPE": "PAYMENT",
                "COMPETITION_ID": _clean(order.get("COMPETITION_ID")),
                "ORDER_ID": order_id,
                "PAYMENT_ID": _clean(payment.get("PAYMENT_ID")),
                "REFUND_ID": "",
                "ENTRY_ID": _clean(payment.get("ENTRY_ID")),
                "PURPOSE": _clean(payment.get("PAYMENT_PURPOSE")) or "ORIGINAL",
                "CUSTOMER_AMOUNT": _money_float(_d(payment.get("AMOUNT"))),
                "STRIPE_FEE_ACTUAL": (
                    _money_float(_d(payment.get("STRIPE_FEE_ACTUAL")))
                    if _known_money(payment, "STRIPE_FEE_ACTUAL")
                    else None
                ),
                "STRIPE_NET_ACTUAL": (
                    _money_float(_d(payment.get("STRIPE_NET_ACTUAL")))
                    if _known_money(payment, "STRIPE_NET_ACTUAL")
                    else None
                ),
                "STRIPE_BALANCE_TRANSACTION_ID": _clean(payment.get("STRIPE_BALANCE_TRANSACTION_ID")),
                "STRIPE_OBJECT_ID": _clean(payment.get("STRIPE_PAYMENT_INTENT_ID")),
                "STATUS": _clean(payment.get("DISPLAY_STATUS")),
            }
        )

    for refund in refunds:
        if not _is_complete_refund(refund):
            continue
        order_id = _clean(refund.get("ORDER_ID"))
        order = order_map.get(order_id, {})
        rows.append(
            {
                "DATE": _clean(refund.get("COMPLETED_AT")) or _clean(refund.get("UPDATED_AT")),
                "TYPE": "REFUND",
                "COMPETITION_ID": _clean(order.get("COMPETITION_ID")),
                "ORDER_ID": order_id,
                "PAYMENT_ID": _clean(refund.get("PAYMENT_ID")),
                "REFUND_ID": _clean(refund.get("REFUND_ID")),
                "ENTRY_ID": _clean(refund.get("ENTRY_ID")),
                "PURPOSE": _clean(refund.get("REFUND_TYPE")) or "WITHDRAWAL",
                "CUSTOMER_AMOUNT": _money_float(-_d(refund.get("APPROVED_AMOUNT"))),
                "STRIPE_FEE_ACTUAL": (
                    _money_float(_d(refund.get("STRIPE_FEE_ACTUAL")))
                    if _known_money(refund, "STRIPE_FEE_ACTUAL")
                    else None
                ),
                "STRIPE_NET_ACTUAL": (
                    _money_float(_d(refund.get("STRIPE_NET_ACTUAL")))
                    if _known_money(refund, "STRIPE_NET_ACTUAL")
                    else None
                ),
                "STRIPE_BALANCE_TRANSACTION_ID": _clean(refund.get("STRIPE_BALANCE_TRANSACTION_ID")),
                "STRIPE_OBJECT_ID": _clean(refund.get("STRIPE_REFUND_ID")),
                "STATUS": _clean(refund.get("STATUS")),
            }
        )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["DATE", "TYPE"], kind="stable").reset_index(drop=True)


def build_order_financial_summary(
    *,
    orders: list[dict],
    payments: list[dict],
    refunds: list[dict],
) -> pd.DataFrame:
    order_map = {
        _clean(o.get("ORDER_ID")): o
        for o in orders
        if _clean(o.get("ORDER_ID"))
    }
    payments_by_order: dict[str, list[dict]] = defaultdict(list)
    refunds_by_order: dict[str, list[dict]] = defaultdict(list)
    for p in payments:
        if _clean(p.get("ORDER_ID")):
            payments_by_order[_clean(p.get("ORDER_ID"))].append(p)
    for r in refunds:
        if _clean(r.get("ORDER_ID")):
            refunds_by_order[_clean(r.get("ORDER_ID"))].append(r)

    rows = []
    for order_id, order in order_map.items():
        paid_rows = [p for p in payments_by_order.get(order_id, []) if _is_complete_payment(p)]
        refund_rows = [r for r in refunds_by_order.get(order_id, []) if _is_complete_refund(r)]
        paid = sum((_d(p.get("AMOUNT")) for p in paid_rows), Decimal("0"))
        refunded = sum((_d(r.get("APPROVED_AMOUNT")) for r in refund_rows), Decimal("0"))

        stripe_objects = paid_rows + refund_rows
        actual_known = [
            row for row in stripe_objects
            if _known_money(row, "STRIPE_FEE_ACTUAL") and _known_money(row, "STRIPE_NET_ACTUAL")
        ]
        all_known = len(actual_known) == len(stripe_objects)
        stripe_fee = sum((_d(r.get("STRIPE_FEE_ACTUAL")) for r in actual_known), Decimal("0"))
        stripe_net = sum((_d(r.get("STRIPE_NET_ACTUAL")) for r in actual_known), Decimal("0"))

        rows.append(
            {
                "COMPETITION_ID": _clean(order.get("COMPETITION_ID")),
                "ORDER_ID": order_id,
                "ORGANIZATION_ID": _clean(order.get("ORGANIZATION_ID")),
                "ORDER_STATUS": _clean(order.get("STATUS")),
                "ORDER_TOTAL": _money_float(_d(order.get("TOTAL_AMOUNT"))),
                "SUCCESSFUL_PAYMENTS": _money_float(paid),
                "COMPLETED_REFUNDS": _money_float(refunded),
                "CUSTOMER_NET_COLLECTED": _money_float(paid - refunded),
                "STRIPE_FEES_ACTUAL": _money_float(stripe_fee) if all_known else None,
                "STRIPE_NET_SETTLEMENT": _money_float(stripe_net) if all_known else None,
                "STRIPE_FINANCIAL_COVERAGE": f"{len(actual_known)}/{len(stripe_objects)}",
            }
        )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["COMPETITION_ID", "ORDER_ID"]).reset_index(drop=True)


def add_team_subtotals(report: pd.DataFrame) -> pd.DataFrame:
    if report is None or report.empty:
        return pd.DataFrame() if report is None else report.copy()

    numeric_cols = [
        "GROSS",
        "AMOUNT_PAID",
        "AMOUNT_INVOICED",
        "COLLECTED",
        "REFUNDS",
        "STRIPE_FEES",
        "REVENUE",
    ]
    pieces: list[pd.DataFrame] = []

    def _subtotal(frame: pd.DataFrame, label: str) -> dict:
        row = {col: "" for col in report.columns}
        row["TEAM"] = label
        row["ATHLETE_NAME"] = "SUBTOTAL" if label != "GRAND TOTAL" else ""
        for col in numeric_cols:
            series = pd.to_numeric(frame[col], errors="coerce")
            row[col] = float(series.sum()) if not series.isna().any() else None
        row["STRIPE_FINANCIALS_COMPLETE"] = bool(
            frame["STRIPE_FINANCIALS_COMPLETE"].fillna(False).all()
        )
        return row

    for team, frame in report.groupby("TEAM", sort=False, dropna=False):
        pieces.append(frame.copy())
        pieces.append(pd.DataFrame([_subtotal(frame, str(team))]))

    pieces.append(pd.DataFrame([_subtotal(report, "GRAND TOTAL")]))
    return pd.concat(pieces, ignore_index=True, sort=False)
