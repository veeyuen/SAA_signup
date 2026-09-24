from __future__ import annotations

from decimal import Decimal
import secrets
import streamlit as st

CART_KEY = "registration_cart"
CART_COMPETITION_KEY = "cart_competition_id"


def get_cart() -> list[dict]:
    st.session_state.setdefault(CART_KEY, [])
    cart = st.session_state.get(CART_KEY, [])
    if not isinstance(cart, list):
        cart = []
        st.session_state[CART_KEY] = cart
    return cart


def cart_has_items() -> bool:
    return bool(get_cart())


def cart_competition_id() -> str:
    return str(st.session_state.get(CART_COMPETITION_KEY, "") or "").strip()


def add_item(item: dict, competition_id: str) -> None:
    cart = get_cart()
    current_competition = cart_competition_id()
    if cart and current_competition and current_competition != competition_id:
        raise ValueError(
            "The cart already contains entries for another competition. "
            "Clear the cart before changing competition."
        )

    if not cart:
        st.session_state[CART_COMPETITION_KEY] = competition_id

    item = dict(item)
    item.setdefault(
        "cart_item_id",
        "CART-"
        + secrets.token_urlsafe(7).replace("-", "").replace("_", "").upper(),
    )
    cart.append(item)
    st.session_state[CART_KEY] = cart


def remove_item(cart_item_id: str) -> None:
    cart = [
        item
        for item in get_cart()
        if str(item.get("cart_item_id", "")) != str(cart_item_id)
    ]
    st.session_state[CART_KEY] = cart
    if not cart:
        st.session_state.pop(CART_COMPETITION_KEY, None)


def clear_cart() -> None:
    st.session_state[CART_KEY] = []
    st.session_state.pop(CART_COMPETITION_KEY, None)


def flatten_entry_rows() -> list[dict]:
    rows = []
    for item in get_cart():
        for row in item.get("entry_rows", []) or []:
            rows.append(dict(row))
    return rows


def total_amount() -> Decimal:
    total = Decimal("0.00")
    for item in get_cart():
        raw = str(item.get("subtotal", "0.00") or "0.00")
        total += Decimal(raw)
    return total.quantize(Decimal("0.01"))


def total_event_entries() -> int:
    return sum(len(item.get("entry_rows", []) or []) for item in get_cart())
