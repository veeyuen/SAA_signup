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
    """Return the best available full-name representation for one roster row.

    Prefer structured first/other/last fields when both first and last name are
    available.  Some historical roster rows contain a stale or differently
    formatted FULL_NAME even though the structured fields are authoritative.
    """
    first = _text(row.get("FIRST_NAME"))
    other = _text(row.get("OTHER_NAME"))
    last = _text(row.get("LAST_NAME"))
    structured = " ".join(part for part in [first, other, last] if part).strip()
    if first and last:
        return structured

    full = _text(row.get("FULL_NAME"))
    if full:
        return full
    return structured


def _legacy_ic4_from_identifier(identifier: Any, dob: Any = None) -> str:
    """Extract IC last-four from the historical SAA derived ID when safe.

    Historical IDs have the shape ``A123B08``: one name initial, the four IC
    characters, then two birth-year digits.  Future opaque ATHLETE_ID values do
    not match this pattern and are therefore never decoded.  When DOB is known,
    the year suffix must agree before the value is accepted.
    """
    raw = re.sub(r"[^A-Z0-9]", "", _text(identifier).upper())
    match = re.fullmatch(r"[A-Z]([0-9]{3}[A-Z])([0-9]{2})", raw)
    if not match:
        return ""

    dob_key = _date_key(dob)
    if dob_key and dob_key[2:4] != match.group(2):
        return ""
    return match.group(1)


def roster_row_ic_last4(row: Mapping[str, Any]) -> str:
    """Return the best supported IC-last-four representation for a roster row."""
    direct = _last4(
        row.get("NRIC")
        or row.get("IC_LAST4")
        or row.get("NRIC_LAST4")
        or ""
    )
    if re.fullmatch(r"[0-9]{3}[A-Z]", direct.upper()):
        return direct.upper()

    # Legacy UNIQUE_ID encodes the same four characters.  Use it only as a
    # fallback when the direct roster field is absent, and only when its exact
    # historical format (and DOB year, when available) validate.
    for field in ("LEGACY_UNIQUE_ID", "UNIQUE_ID"):
        inferred = _legacy_ic4_from_identifier(row.get(field), row.get("DOB"))
        if inferred:
            return inferred
    return ""


def resolve_nationality_option(value: Any, options: Iterable[Any]) -> str:
    """Resolve roster nationality text to the registration dropdown value.

    Handles exact country names as well as historical values such as
    ``SGP Singapore`` without inventing a country when no supported option can
    be established.  If no dropdown option matches, return the raw text so the
    caller can expose it through the existing override mechanism.
    """
    raw = _text(value)
    if not raw:
        return ""

    option_values = [_text(option) for option in (options or []) if _text(option)]
    raw_key = _normalise_search(raw)
    for option in option_values:
        if raw_key == _normalise_search(option):
            return option

    # Common Singapore codes used in historical athletics data.
    if raw_key in {"sg", "sin", "sgp", "singapore", "sg singapore", "sin singapore", "sgp singapore"}:
        for option in option_values:
            if _normalise_search(option) == "singapore":
                return option

    # Generic ``XXX Country Name`` form.  Only strip a short alphabetic code
    # when the remainder exactly equals one of the configured options.
    tokens = raw_key.split()
    if len(tokens) >= 2 and 2 <= len(tokens[0]) <= 3 and tokens[0].isalpha():
        remainder = " ".join(tokens[1:])
        for option in option_values:
            if remainder == _normalise_search(option):
                return option

    return raw



def nationality_widget_plan(
    *,
    current_value: Any,
    override_value: Any,
    configured_options: Iterable[Any],
    selected_prefill_value: Any = "",
    selected_existing: bool = False,
) -> tuple[list[str], str, int]:
    """Return deterministic options/default for the Nationality selectbox.

    Streamlit can retain the previous blank widget value across a rerun even
    after an existing athlete has been selected.  The selected-athlete snapshot
    is therefore allowed to seed the widget only when the current widget value
    is blank.  A non-blank current value always wins so a user's later edit is
    preserved.
    """
    configured = []
    for option in configured_options or []:
        text = _text(option)
        if text and text not in configured:
            configured.append(text)

    current = _text(current_value)
    override = _text(override_value)
    selected_prefill = _text(selected_prefill_value)

    seed_raw = current
    if not seed_raw and selected_existing and selected_prefill:
        seed_raw = selected_prefill
    if not seed_raw and override:
        seed_raw = override

    desired = resolve_nationality_option(seed_raw, configured) if seed_raw else ""

    options = [""] + configured
    for extra in (override, desired):
        if extra and extra not in options:
            options.insert(1, extra)

    index = options.index(desired) if desired in options else 0
    return options, desired, index


def roster_prefill_values(row: Mapping[str, Any], nationality_options: Iterable[Any]) -> dict[str, Any]:
    """Build the canonical one-time form prefill for a selected roster athlete.

    Keeping this transformation pure makes the Streamlit hand-off deterministic
    and testable.  In particular, historical roster values such as
    ``SGP Singapore`` and legacy IDs such as ``V852E96`` are normalised before
    widget state is touched.
    """
    first = _text(row.get("FIRST_NAME"))
    other = _text(row.get("OTHER_NAME"))
    last = _text(row.get("LAST_NAME"))
    full = roster_row_name(row)

    passport_name = _text(
        row.get("NAME_PASSPORT")
        or row.get("NAME_AS_PER_NRIC_PASSPORT")
        or row.get("NAME AS PER NRIC/PASSPORT")
        or full
    )
    ic_last4 = roster_row_ic_last4(row)

    gender_raw = _text(row.get("GENDER")).upper()
    gender = ""
    if gender_raw in {"M", "MALE"}:
        gender = "Male"
    elif gender_raw in {"F", "FEMALE"}:
        gender = "Female"

    nationality_raw = _text(row.get("NATIONALITY"))
    options = [_text(x) for x in (nationality_options or []) if _text(x)]
    nationality = resolve_nationality_option(nationality_raw, options)
    nationality_override = "" if nationality in options else (nationality or nationality_raw)

    sgpr_raw = _text(
        row.get("SINGAPORE_PR")
        or row.get("SG_PR")
        or row.get("PR_STATUS")
    ).casefold()
    nationality_cf = nationality_raw.casefold()
    singapore_pr = (
        sgpr_raw in {"yes", "y", "true", "1", "pr", "singapore pr", "sg pr"}
        or nationality_cf in {"singapore pr", "sg pr"}
    )

    unique_id = _text(
        row.get("ATHLETE_ID")
        or row.get("UNIQUE_ID")
        or row.get("LEGACY_UNIQUE_ID")
    )
    email = _text(row.get("EMAIL") or row.get("Email"))
    contact = _text(
        row.get("CONTACT_NUMBER")
        or row.get("CONTACT")
        or row.get("MOBILE")
        or row.get("PHONE")
    )

    return {
        "first_name": first or other,
        "other_name": other,
        "last_name": last,
        "full_name": full,
        "name_passport": passport_name,
        "ic_last4": ic_last4,
        "dob_raw": row.get("DOB"),
        "gender": gender,
        "nationality": nationality or nationality_raw,
        "nationality_override": nationality_override,
        "singapore_pr": bool(singapore_pr),
        "unique_id": unique_id,
        "email": email,
        "contact_number": contact,
    }

def sync_auto_full_name(
    *,
    typed_full_name: Any,
    current_full_name: Any,
    previous_auto_full_name: Any,
) -> tuple[str, str]:
    """Return the next Full Name value and auto-source marker.

    Structured name edits keep updating Full Name while it still equals the
    previous auto-generated value. A genuine manual Full Name override is
    preserved.
    """
    typed = _text(typed_full_name)
    current = _text(current_full_name)
    previous_auto = _text(previous_auto_full_name)

    if typed and (not current or current == previous_auto):
        return typed, typed
    if not typed and current == previous_auto:
        return "", ""
    return current, previous_auto


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
    * REVIEW requires any two of those three identity signals.

    The form's auto-generated legacy UNIQUE_ID is deliberately *not* treated as
    independent evidence because it is itself derived from name/IC/DOB input.
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
    # ``derived_unique_id`` is retained as a backwards-compatible keyword only.
    # Do not count it as an independent match signal; it is generated from the
    # same fields already being compared below.
    _ = derived_unique_id

    matches: list[tuple[int, int, AthleteIdentityMatch]] = []

    for index, row in enumerate(rows or []):
        row_names = _row_name_keys(row)
        row_dob = _date_key(row.get("DOB"))
        row_ic4 = _normalise_search(roster_row_ic_last4(row))
        row_gender = _gender_key(row.get("GENDER"))

        name_match = bool(candidate_names and row_names and candidate_names & row_names)
        dob_match = bool(candidate_dob and row_dob and candidate_dob == row_dob)
        ic_match = bool(candidate_ic4 and row_ic4 and candidate_ic4 == row_ic4)
        gender_conflict = bool(
            candidate_gender and row_gender and candidate_gender != row_gender
        )

        core_count = sum((name_match, dob_match, ic_match))
        classification = ""
        score = 0

        if core_count == 3 and not gender_conflict:
            classification = IDENTITY_STRONG_MATCH
            score = 100
        elif core_count >= 2:
            classification = IDENTITY_REVIEW
            score = 60 + core_count * 10

        if not classification:
            continue

        reasons = []
        if name_match:
            reasons.append("NAME_MATCH")
        if dob_match:
            reasons.append("DOB_MATCH")
        if ic_match:
            reasons.append("IC_LAST4_MATCH")
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
