"""Athlete-selection helpers for the competition registration UI.

The registration page supports two reversible interfaces:

SEARCH_FIRST
    Search the roster first, choose an existing athlete or explicitly start a
    new-athlete entry, then show the registration form.

LEGACY
    Keep the existing behaviour where the user types name fields first and the
    roster-match selector appears afterwards.

This module is intentionally Streamlit-free so search/mode behaviour can be
unit-tested independently from the page.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Any


SEARCH_FIRST = "SEARCH_FIRST"
LEGACY = "LEGACY"
VALID_UI_MODES = {SEARCH_FIRST, LEGACY}


def normalize_ui_mode(value: Any, default: str = SEARCH_FIRST) -> str:
    """Return SEARCH_FIRST or LEGACY, falling back safely for bad config."""
    candidate = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "SEARCH": SEARCH_FIRST,
        "SEARCHFIRST": SEARCH_FIRST,
        "NEW": SEARCH_FIRST,
        "OLD": LEGACY,
        "PREVIOUS": LEGACY,
    }
    candidate = aliases.get(candidate, candidate)
    if candidate in VALID_UI_MODES:
        return candidate

    fallback = str(default or SEARCH_FIRST).strip().upper()
    return fallback if fallback in VALID_UI_MODES else SEARCH_FIRST


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_search(value: Any) -> str:
    """Casefold and collapse punctuation/whitespace for forgiving search."""
    raw = _text(value).casefold()
    raw = re.sub(r"[^\w]+", " ", raw, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _last4(value: Any) -> str:
    raw = re.sub(r"\s+", "", _text(value).upper())
    return raw[-4:] if len(raw) >= 4 else raw


def roster_row_name(row: Mapping[str, Any]) -> str:
    """Return the best available full-name representation for one roster row."""
    full = _text(row.get("FULL_NAME"))
    if full:
        return full
    parts = [
        _text(row.get("FIRST_NAME")),
        _text(row.get("OTHER_NAME")),
        _text(row.get("LAST_NAME")),
    ]
    return " ".join(part for part in parts if part).strip()


def roster_search_score(row: Mapping[str, Any], query: Any) -> int:
    """Score one roster row for a user-entered search query.

    Exact athlete/current-ID matches rank first, followed by exact NRIC-last4
    and team-code matches, then name phrase/token matches. Full NRIC values are
    never required for matching; only the last four characters are considered.
    """
    q = _normalise_search(query)
    if len(q) < 2:
        return 0

    tokens = [token for token in q.split(" ") if token]

    first = _normalise_search(row.get("FIRST_NAME"))
    other = _normalise_search(row.get("OTHER_NAME"))
    last = _normalise_search(row.get("LAST_NAME"))
    full = _normalise_search(roster_row_name(row))
    name_hay = " ".join(part for part in [full, first, other, last] if part)

    identifiers = [
        _normalise_search(row.get("ATHLETE_ID")),
        _normalise_search(row.get("UNIQUE_ID")),
        _normalise_search(row.get("LEGACY_UNIQUE_ID")),
    ]
    identifiers = [value for value in identifiers if value]

    nric_last4 = _normalise_search(_last4(row.get("NRIC")))
    team_code = _normalise_search(row.get("TEAM_CODE"))
    team_name = _normalise_search(row.get("TEAM_NAME"))

    score = 0

    if q in identifiers:
        score += 100
    elif any(q and q in value for value in identifiers):
        score += 45

    if nric_last4 and q == nric_last4:
        score += 70
    elif nric_last4 and q in nric_last4:
        score += 20

    if team_code and q == team_code:
        score += 35
    elif team_name and q in team_name:
        score += 18

    if full and q == full:
        score += 80
    elif q and q in name_hay:
        score += 30

    if tokens:
        matched_tokens = sum(1 for token in tokens if token in name_hay)
        score += matched_tokens * 8
        if matched_tokens == len(tokens):
            score += 20

    return score


def search_roster_rows(
    rows: Iterable[Mapping[str, Any]],
    query: Any,
    *,
    limit: int = 8,
) -> list[Mapping[str, Any]]:
    """Return roster candidates ordered by relevance, preserving stable ties."""
    scored: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, row in enumerate(rows or []):
        score = roster_search_score(row, query)
        if score > 0:
            scored.append((score, index, row))

    scored.sort(key=lambda item: (-item[0], item[1]))
    if limit is None or limit <= 0:
        return [row for _score, _index, row in scored]
    return [row for _score, _index, row in scored[:limit]]
