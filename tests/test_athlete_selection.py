from signup.athlete_selection import (
    LEGACY,
    SEARCH_FIRST,
    normalize_ui_mode,
    roster_row_name,
    roster_row_ic_last4,
    resolve_nationality_option,
    sync_auto_full_name,
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


def test_auto_generated_legacy_id_is_not_independent_duplicate_evidence():
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
    assert matches == []


def test_legacy_unique_id_can_supply_missing_roster_ic4_for_identity_check():
    rows = [
        {
            "FIRST_NAME": "Veronica Shanti",
            "LAST_NAME": "Pereira",
            "DOB": "1996-09-20",
            "UNIQUE_ID": "V852E96",
            "GENDER": "Female",
        }
    ]
    assert roster_row_ic_last4(rows[0]) == "852E"
    matches = find_new_athlete_identity_matches(
        rows,
        first_name="Veronica Shanti",
        last_name="Pereira",
        birth_date="20/09/1996",
        ic_last4="852E",
        gender="Female",
    )
    assert len(matches) == 1
    assert matches[0].classification == IDENTITY_STRONG_MATCH
    assert set(matches[0].reasons) >= {"NAME_MATCH", "DOB_MATCH", "IC_LAST4_MATCH"}


def test_legacy_unique_id_ic4_fallback_rejects_birth_year_mismatch():
    row = {
        "DOB": "1997-09-20",
        "UNIQUE_ID": "V852E96",
    }
    assert roster_row_ic_last4(row) == ""


def test_roster_row_name_prefers_complete_structured_name_over_stale_full_name():
    row = {
        "FIRST_NAME": "VERONICA SHANTI",
        "LAST_NAME": "PEREIRA",
        "FULL_NAME": "VERONICA SHANTI",
    }
    assert roster_row_name(row) == "VERONICA SHANTI PEREIRA"


def test_nationality_resolves_code_plus_country_to_dropdown_country():
    options = ["Singapore", "Malaysia", "Japan"]
    assert resolve_nationality_option("SGP Singapore", options) == "Singapore"
    assert resolve_nationality_option("Singapore", options) == "Singapore"


def test_nationality_unmatched_value_is_preserved_for_override():
    assert resolve_nationality_option("XYZ Exampleland", ["Singapore"]) == "XYZ Exampleland"


def test_auto_full_name_tracks_structured_edits_until_user_overrides():
    value, marker = sync_auto_full_name(
        typed_full_name="Pereira",
        current_full_name="",
        previous_auto_full_name="",
    )
    assert (value, marker) == ("Pereira", "Pereira")

    value, marker = sync_auto_full_name(
        typed_full_name="Veronica Shanti Pereira",
        current_full_name=value,
        previous_auto_full_name=marker,
    )
    assert (value, marker) == (
        "Veronica Shanti Pereira",
        "Veronica Shanti Pereira",
    )

    value, marker = sync_auto_full_name(
        typed_full_name="Veronica S Pereira",
        current_full_name="Veronica Shanti P.",
        previous_auto_full_name=marker,
    )
    assert value == "Veronica Shanti P."
    assert marker == "Veronica Shanti Pereira"


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


def test_roster_prefill_normalises_veronica_recall_fields():
    from signup.athlete_selection import roster_prefill_values

    row = {
        "FIRST_NAME": "VERONICA SHANTI",
        "OTHER_NAME": "",
        "LAST_NAME": "PEREIRA",
        "FULL_NAME": "VERONICA SHANTI",
        "DOB": "1996-09-20",
        "UNIQUE_ID": "V852E96",
        "GENDER": "Female",
        "NATIONALITY": "SGP Singapore",
    }
    prefill = roster_prefill_values(row, ["Singapore", "Malaysia", "Japan"])
    assert prefill["full_name"] == "VERONICA SHANTI PEREIRA"
    assert prefill["name_passport"] == "VERONICA SHANTI PEREIRA"
    assert prefill["ic_last4"] == "852E"
    assert prefill["nationality"] == "Singapore"
    assert prefill["nationality_override"] == ""
    assert prefill["unique_id"] == "V852E96"


def test_roster_prefill_preserves_unconfigured_nationality_as_override():
    from signup.athlete_selection import roster_prefill_values

    prefill = roster_prefill_values(
        {"FIRST_NAME": "A", "LAST_NAME": "B", "NATIONALITY": "XYZ Exampleland"},
        ["Singapore"],
    )
    assert prefill["nationality"] == "XYZ Exampleland"
    assert prefill["nationality_override"] == "XYZ Exampleland"
