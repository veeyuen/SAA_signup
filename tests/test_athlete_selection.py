from signup.athlete_selection import (
    LEGACY,
    SEARCH_FIRST,
    normalize_ui_mode,
    roster_row_name,
    search_roster_rows,
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
