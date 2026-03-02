"""
Name suggestion helpers for Streamlit inputs.

This is adapted from the user's database-search approach (substring matching on a canonicalized name)
as seen in the provided snippet. fileciteturn0file0

Design:
- Works with any list of candidate strings (e.g., loaded from DB / CSV / hardcoded).
- Uses fast substring matching first; falls back to simple fuzzy matching.
- Provides a small "Suggestions" selectbox that can populate the text_input.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable, List

import streamlit as st


_WS_RE = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """Lowercase-ish (casefold), trim, collapse whitespace."""
    s = (s or "").strip()
    s = _WS_RE.sub(" ", s)
    return s.casefold()


def unique_preserve(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        k = normalize_text(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def suggest_matches(query: str, candidates: List[str], limit: int = 10) -> List[str]:
    """Return up to `limit` candidate strings matching query."""
    qn = normalize_text(query)
    if not qn:
        return []

    # Substring matches (fast + predictable)
    scored = []
    for c in candidates:
        cn = normalize_text(c)
        pos = cn.find(qn)
        if pos >= 0:
            scored.append((pos, len(cn), c))

    if scored:
        scored.sort(key=lambda t: (t[0], t[1], normalize_text(t[2])))
        return [c for *_rest, c in scored[:limit]]

    # Fallback fuzzy matches
    pool = [normalize_text(c) for c in candidates]
    norm_to_orig = {normalize_text(c): c for c in candidates}
    fuzzy = difflib.get_close_matches(qn, pool, n=limit, cutoff=0.75)
    return [norm_to_orig.get(n, n) for n in fuzzy]


def suggested_text_input(
    label: str,
    key: str,
    candidates: List[str],
    *,
    placeholder: str | None = None,
    help_text: str | None = None,
    limit: int = 10,
):
    """Text input with a suggestion selectbox underneath.

    - User types freely.
    - If there are matches, user can pick one to populate the field.
    """
    st.text_input(label, key=key, placeholder=placeholder, help=help_text)

    current = st.session_state.get(key, "") or ""
    matches = suggest_matches(current, candidates, limit=limit) if current else []

    if matches:
        sel_key = f"{key}__suggestion"
        options = ["(keep typed)"] + matches
        if st.session_state.get(sel_key) not in options:
            st.session_state[sel_key] = "(keep typed)"

        chosen = st.selectbox(
            f"Suggestions for {label}",
            options=options,
            key=sel_key,
        )
        if chosen and chosen != "(keep typed)":
            # Avoid modifying the widget key after it is instantiated.
            # Store a pending update, applied at the top of the next rerun.
            st.session_state[f"{key}__pending"] = chosen
            st.session_state[sel_key] = "(keep typed)"
            st.rerun()
