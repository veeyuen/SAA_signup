from signup.athlete_selection import (
    LEGACY,
    SEARCH_FIRST,
    normalize_ui_mode,
    roster_row_name,
    search_roster_rows,
    find_new_athlete_identity_matches,
    IDENTITY_STRONG_MATCH,
    IDENTITY_REVIEW,
)


ROWS = [
    {
        "FIRST_NAME": "Wei Ming",
        "OTHER_NAME": "John",
        "LAST_NAME": "Tan",
        "FULL_NAME": "Wei Ming John Tan",
        "NRIC": "S123123A",
        "UNIQUE_ID": "W123A08",
        "TEAM_CODE": "TAC",
        "TEAM_NAME": "Test Athletics Club",
    },
    {
        "FIRST_NAME": "Sarah",
        "OTHER_NAME": "",
        "LAST_NAME": "Wong",
        "FULL_NAME": "Sarah Wong",
        "NRIC": "T999456H",
        "UNIQUE_ID": "S456H07",
        "TEAM_CODE": "ASC",
        "TEAM_NAME": "Another Sports Club",
    },
]


def test_mode_defaults_to_search_first():
    assert normalize_ui_mode("") == SEARCH_FIRST
    assert normalize_ui_mode("search-first") == SEARCH_FIRST
    assert normalize_ui_mode("legacy") == LEGACY
    assert normalize_ui_mode("unexpected") == SEARCH_FIRST


def test_roster_row_name_uses_structured_name_when_full_name_missing():
    row = {"FIRST_NAME": "Wei", "OTHER_NAME": "Ming", "LAST_NAME": "Tan"}
    assert roster_row_name(row) == "Wei Ming Tan"


def test_search_can_find_reordered_name_tokens():
    matches = search_roster_rows(ROWS, "tan wei")
    assert matches[0]["UNIQUE_ID"] == "W123A08"


def test_search_exact_unique_id_ranks_first():
    matches = search_roster_rows(ROWS, "S456H07")
    assert matches[0]["FIRST_NAME"] == "Sarah"


def test_search_can_find_nric_last4_without_full_nric_query():
    matches = search_roster_rows(ROWS, "123A")
    assert matches[0]["LAST_NAME"] == "Tan"


def test_search_can_find_team():
    matches = search_roster_rows(ROWS, "Another Sports")
    assert matches[0]["FIRST_NAME"] == "Sarah"


def test_short_query_does_not_return_candidates():
    assert search_roster_rows(ROWS, "w") == []


def test_future_athlete_id_is_searchable_without_ui_change():
    rows = [
        {
            "ATHLETE_ID": "SAA-7K3M9Q2DX4PF",
            "FIRST_NAME": "Wei Ming",
            "LAST_NAME": "Tan",
        }
    ]
    matches = search_roster_rows(rows, "SAA-7K3M9Q2DX4PF")
    assert matches[0]["ATHLETE_ID"] == "SAA-7K3M9Q2DX4PF"


def test_unicode_name_search_is_supported():
    rows = [
        {
            "FIRST_NAME": "José",
            "LAST_NAME": "García",
            "FULL_NAME": "José García",
        }
    ]
    matches = search_roster_rows(rows, "josé")
    assert matches[0]["LAST_NAME"] == "García"



def test_new_athlete_exact_canonical_identity_is_strong_match():
    rows = [
        {
            "FIRST_NAME": "Wei Ming",
            "OTHER_NAME": "John",
            "LAST_NAME": "Tan",
            "FULL_NAME": "Wei Ming John Tan",
            "DOB": "2008-04-15",
            "NRIC": "S123123A",
            "GENDER": "Male",
            "UNIQUE_ID": "W123A08",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Tan",
        other_name="John Wei Ming",
        last_name="",
        name_passport="Tan John Wei Ming",
        birth_date="15/04/2008",
        ic_last4="123A",
        gender="Male",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_STRONG_MATCH
    assert set(matches[0].reasons) >= {"NAME_MATCH", "DOB_MATCH", "IC_LAST4_MATCH"}


def test_new_athlete_name_and_dob_only_requires_review_not_auto_merge():
    rows = [
        {
            "FULL_NAME": "Sarah Wong",
            "DOB": "2007-09-22",
            "NRIC": "T999456H",
            "GENDER": "Female",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Sarah",
        last_name="Wong",
        birth_date="2007-09-22",
        ic_last4="111A",
        gender="Female",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_REVIEW
    assert "NAME_MATCH" in matches[0].reasons
    assert "DOB_MATCH" in matches[0].reasons
    assert "IC_LAST4_MATCH" not in matches[0].reasons


def test_new_athlete_ic4_and_dob_only_requires_review():
    rows = [
        {
            "FULL_NAME": "Different Person",
            "DOB": "2008-04-15",
            "NRIC": "S123123A",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Wei Ming",
        last_name="Tan",
        birth_date="2008-04-15",
        ic_last4="123A",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_REVIEW


def test_new_athlete_single_name_signal_does_not_block_creation():
    rows = [
        {
            "FULL_NAME": "Alex Tan",
            "DOB": "2001-01-01",
            "NRIC": "S111111A",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Alex",
        last_name="Tan",
        birth_date="2005-05-05",
        ic_last4="999Z",
    )
    assert matches == []


def test_new_athlete_exact_legacy_id_is_review_signal_not_silent_merge():
    rows = [
        {
            "FULL_NAME": "Existing Athlete",
            "DOB": "2008-04-15",
            "NRIC": "S123123A",
            "UNIQUE_ID": "E123A08",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Different",
        last_name="Name",
        birth_date="2001-01-01",
        ic_last4="999Z",
        derived_unique_id="E123A08",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_REVIEW
    assert "CURRENT_ID_MATCH" in matches[0].reasons


def test_gender_conflict_downgrades_full_identity_match_to_review():
    rows = [
        {
            "FULL_NAME": "Wei Ming Tan",
            "DOB": "2008-04-15",
            "NRIC": "S123123A",
            "GENDER": "Female",
        }
    ]
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Wei Ming",
        last_name="Tan",
        birth_date="2008-04-15",
        ic_last4="123A",
        gender="Male",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_REVIEW
    assert "GENDER_CONFLICT" in matches[0].reasons
