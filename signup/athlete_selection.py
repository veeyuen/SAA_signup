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

# ---------------------------------------------------------------------------
# New-athlete duplicate prevention
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from datetime import date, datetime, timedelta

IDENTITY_STRONG_MATCH = "STRONG_MATCH"
IDENTITY_REVIEW = "REVIEW"


@dataclass(frozen=True)
class AthleteIdentityMatch:
    """One existing roster row that may represent a proposed new athlete."""

    classification: str
    row: Mapping[str, Any]
    reasons: tuple[str, ...]
    score: int


def _canonical_name_key(value: Any) -> str:
    """Return a name key that is insensitive to token order.

    This deliberately does not do fuzzy spelling. It only removes punctuation,
    case and field-order differences so ``Tan Wei Ming`` and ``Wei Ming Tan``
    compare as the same exact token set.
    """
    normalized = _normalise_search(value)
    if not normalized:
        return ""
    return " ".join(sorted(token for token in normalized.split(" ") if token))


def _candidate_name_keys(
    *,
    first_name: Any = "",
    other_name: Any = "",
    last_name: Any = "",
    name_passport: Any = "",
) -> set[str]:
    values = []
    passport = _text(name_passport)
    if passport:
        values.append(passport)
    structured = " ".join(
        part
        for part in [_text(first_name), _text(other_name), _text(last_name)]
        if part
    ).strip()
    if structured:
        values.append(structured)
    return {key for key in (_canonical_name_key(value) for value in values) if key}


def _row_name_keys(row: Mapping[str, Any]) -> set[str]:
    values = [
        _text(row.get("NAME_PASSPORT")),
        _text(row.get("NAME_AS_PER_NRIC_PASSPORT")),
        _text(row.get("NAME AS PER NRIC/PASSPORT")),
        _text(row.get("FULL_NAME")),
        roster_row_name(row),
    ]
    return {key for key in (_canonical_name_key(value) for value in values) if key}


def _date_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and value > 0:
        # Google Sheets / Excel serial date, matching the roster loader.
        if value < 60000:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()

    raw = _text(value)
    if not raw:
        return ""

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    # Prefer unambiguous ISO, then the common day-first formats used by the app.
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _gender_key(value: Any) -> str:
    normalized = _normalise_search(value)
    if normalized in {"m", "male"}:
        return "M"
    if normalized in {"f", "female"}:
        return "F"
    return ""


def find_new_athlete_identity_matches(
    rows: Iterable[Mapping[str, Any]],
    *,
    first_name: Any = "",
    other_name: Any = "",
    last_name: Any = "",
    name_passport: Any = "",
    birth_date: Any = None,
    ic_last4: Any = "",
    gender: Any = "",
    derived_unique_id: Any = "",
    limit: int = 5,
) -> list[AthleteIdentityMatch]:
    """Find existing athletes that make a proposed *new* athlete unsafe to create.

    Matching is intentionally conservative:

    * STRONG_MATCH requires exact canonical name + full DOB + NRIC last four.
    * REVIEW requires any two of those three identity signals, or an exact
      current/legacy ID match.
    * A single common signal (for example name alone) never blocks creation.

    A gender conflict downgrades what would otherwise be a strong match to
    REVIEW rather than silently deciding that either record is correct.
    """
    candidate_names = _candidate_name_keys(
        first_name=first_name,
        other_name=other_name,
        last_name=last_name,
        name_passport=name_passport,
    )
    candidate_dob = _date_key(birth_date)
    candidate_ic4 = _normalise_search(_last4(ic_last4))
    candidate_gender = _gender_key(gender)
    candidate_id = _normalise_search(derived_unique_id)

    matches: list[tuple[int, int, AthleteIdentityMatch]] = []

    for index, row in enumerate(rows or []):
        row_names = _row_name_keys(row)
        row_dob = _date_key(row.get("DOB"))
        row_ic4 = _normalise_search(
            _last4(
                row.get("NRIC")
                or row.get("IC_LAST4")
                or row.get("NRIC_LAST4")
                or ""
            )
        )
        row_gender = _gender_key(row.get("GENDER"))
        row_ids = {
            _normalise_search(row.get(field))
            for field in ("ATHLETE_ID", "UNIQUE_ID", "LEGACY_UNIQUE_ID")
            if _normalise_search(row.get(field))
        }

        name_match = bool(candidate_names and row_names and candidate_names & row_names)
        dob_match = bool(candidate_dob and row_dob and candidate_dob == row_dob)
        ic_match = bool(candidate_ic4 and row_ic4 and candidate_ic4 == row_ic4)
        id_match = bool(candidate_id and candidate_id in row_ids)
        gender_conflict = bool(
            candidate_gender and row_gender and candidate_gender != row_gender
        )

        core_count = sum((name_match, dob_match, ic_match))
        classification = ""
        score = 0

        if core_count == 3 and not gender_conflict:
            classification = IDENTITY_STRONG_MATCH
            score = 100
        elif core_count >= 2 or id_match:
            classification = IDENTITY_REVIEW
            score = 60 + core_count * 10 + (10 if id_match else 0)

        if not classification:
            continue

        reasons = []
        if name_match:
            reasons.append("NAME_MATCH")
        if dob_match:
            reasons.append("DOB_MATCH")
        if ic_match:
            reasons.append("IC_LAST4_MATCH")
        if id_match:
            reasons.append("CURRENT_ID_MATCH")
        if gender_conflict:
            reasons.append("GENDER_CONFLICT")

        matches.append(
            (
                score,
                index,
                AthleteIdentityMatch(
                    classification=classification,
                    row=row,
                    reasons=tuple(reasons),
                    score=score,
                ),
            )
        )

    matches.sort(key=lambda item: (-item[0], item[1]))
    resolved = [match for _score, _index, match in matches]
    if limit is None or limit <= 0:
        return resolved
    return resolved[:limit]
