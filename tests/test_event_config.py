import pandas as pd
import pytest

from signup.pilot_config import PilotConfigError, PilotConfigRepository
from signup.competition_rules import validate_athlete_selection


COMP_ID = "ACM5_2026"


def competitions_df():
    return pd.DataFrame([
        {"COMPETITION_ID": COMP_ID},
        {"COMPETITION_ID": "NO_EVENT_CONFIG"},
    ])


def divisions_df():
    return pd.DataFrame([
        {"DIVISION_CODE": "U15", "DIVISION_NAME": "U15", "MIN_AGE": "13", "MAX_AGE": "15", "DISPLAY_ORDER": 10, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U18", "DIVISION_NAME": "U18", "MIN_AGE": "16", "MAX_AGE": "17", "DISPLAY_ORDER": 20, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U20", "DIVISION_NAME": "U20", "MIN_AGE": "18", "MAX_AGE": "19", "DISPLAY_ORDER": 30, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "OPEN", "DIVISION_NAME": "Open", "MIN_AGE": "16", "MAX_AGE": "", "DISPLAY_ORDER": 40, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "Novice", "DIVISION_NAME": "Novice", "MIN_AGE": "", "MAX_AGE": "", "DISPLAY_ORDER": 50, "ACTIVE": "FALSE"},
        {"DIVISION_CODE": "Intermediate", "DIVISION_NAME": "Intermediate", "MIN_AGE": "", "MAX_AGE": "", "DISPLAY_ORDER": 60, "ACTIVE": "FALSE"},
        {"DIVISION_CODE": "Advance", "DIVISION_NAME": "Advance", "MIN_AGE": "", "MAX_AGE": "", "DISPLAY_ORDER": 70, "ACTIVE": "FALSE"},
    ])


def event_config_df():
    rows = []
    order = 10
    def add(division, gender, *events):
        nonlocal order
        for event in events:
            rows.append({"ACTIVE": "TRUE", "COMPETITION_ID": COMP_ID, "DIVISION": division, "GENDER": gender, "EVENT": event, "EVENT_CLASS": "", "DISPLAY_ORDER": order})
            order += 10
    add("U15", "Male", "100m", "110m H")
    add("U15", "Female", "100m", "80m H")
    add("U18", "Male", "100m", "2000m SC")
    add("U20", "Male", "100m", "3000m SC")
    add("U20", "Female", "100m", "2000m SC")
    add("OPEN", "Male", "10000m Race Walk", "110m H")
    add("OPEN", "Female", "10000m Race Walk", "100m H")
    add("Novice", "Any", "High Jump", "Pole Vault")
    add("Intermediate", "Any", "High Jump", "Pole Vault")
    add("Advance", "Any", "High Jump", "Pole Vault")
    return pd.DataFrame(rows)


def repo(event_config=None):
    r = PilotConfigRepository("test-url")
    tables = {
        "COMPETITIONS": competitions_df(),
        "DIVISIONS": divisions_df(),
        "EVENT_CONFIG": event_config_df() if event_config is None else event_config,
    }
    r.table = lambda worksheet, fresh=False: tables[worksheet].copy()
    return r


def names(r, division, gender):
    return [name for name, _ in r.event_options(COMP_ID, gender, division)]


def test_acm5_resolves_event_config_and_expected_matrix():
    r = repo()
    assert r.event_config_rows(COMP_ID) is not None
    assert "110m H" in names(r, "U15", "Male")
    assert "80m H" in names(r, "U15", "Female")
    assert "110m H" not in names(r, "U15", "Female")
    assert "2000m SC" in names(r, "U18", "Male")
    assert "3000m SC" in names(r, "U20", "Male")
    assert "2000m SC" in names(r, "U20", "Female")
    assert {"10000m Race Walk", "110m H"}.issubset(names(r, "OPEN", "Male"))
    assert {"10000m Race Walk", "100m H"}.issubset(names(r, "OPEN", "Female"))


@pytest.mark.parametrize("division", ["Novice", "Intermediate", "Advance"])
def test_skill_divisions_expose_only_high_jump_and_pole_vault(division):
    assert names(repo(), division, "Female") == ["High Jump", "Pole Vault"]
    assert names(repo(), division, "Male") == ["High Jump", "Pole Vault"]


def test_inactive_event_is_excluded():
    df = event_config_df()
    df.loc[(df.DIVISION == "U15") & (df.GENDER == "Female") & (df.EVENT == "80m H"), "ACTIVE"] = "FALSE"
    assert "80m H" not in names(repo(df), "U15", "Female")


def test_unknown_competition_id_is_configuration_error():
    df = event_config_df()
    df.loc[len(df)] = ["TRUE", "UNKNOWN", "U15", "Male", "100m", "", 999]
    with pytest.raises(PilotConfigError, match="unknown COMPETITION_ID"):
        repo(df).event_config_rows(COMP_ID)


def test_duplicate_event_config_is_configuration_error():
    df = event_config_df()
    df.loc[len(df)] = df.iloc[0]
    with pytest.raises(PilotConfigError, match="duplicates"):
        repo(df).event_config_rows(COMP_ID)


@pytest.mark.parametrize("field,value,match", [
    ("EVENT", "", "missing EVENT"),
    ("ACTIVE", "perhaps", "invalid ACTIVE"),
    ("DISPLAY_ORDER", "ten", "invalid DISPLAY_ORDER"),
])
def test_invalid_required_configuration_is_detected(field, value, match):
    df = event_config_df()
    df.loc[0, field] = value
    with pytest.raises(PilotConfigError, match=match):
        repo(df).event_config_rows(COMP_ID)


def test_event_config_division_must_exist_in_divisions_master():
    df = event_config_df()
    df.loc[df["DIVISION"].eq("U15"), "DIVISION"] = "U15_typo"
    with pytest.raises(PilotConfigError, match="unknown DIVISION='U15_typo'"):
        repo(df).event_config_rows(COMP_ID)


def test_event_config_division_case_and_whitespace_variants_are_canonicalized():
    df = event_config_df()
    df.loc[df["DIVISION"].eq("OPEN"), "DIVISION"] = "  Open  "
    rows = repo(df).event_config_rows("  acm5_2026  ")
    assert rows is not None
    assert set(rows.loc[rows["DIVISION"].eq("OPEN"), "DIVISION"]) == {"OPEN"}
    assert "100m H" in names(repo(df), " open ", " female ")
    assert "110m H" in names(repo(df), "Open", "MALE")


def test_event_config_competition_id_case_and_whitespace_variants_are_canonicalized():
    df = event_config_df()
    df["COMPETITION_ID"] = "  acm5_2026  "
    r = repo(df)
    rows = r.event_config_rows("ACM5_2026")
    assert rows is not None
    assert set(rows["COMPETITION_ID"]) == {"ACM5_2026"}
    assert "80m H" in names(r, "U15", "Female")


def test_event_config_gender_case_and_whitespace_variants_are_canonicalized():
    df = event_config_df()
    df.loc[df["GENDER"].eq("Female"), "GENDER"] = "  fEmAlE  "
    df.loc[df["GENDER"].eq("Male"), "GENDER"] = " mAlE "
    rows = repo(df).event_config_rows(COMP_ID)
    assert rows is not None
    assert set(rows["GENDER"]).issubset({"Male", "Female", "Any"})
    assert "80m H" in names(repo(df), "u15", "FEMALE")
    assert "110m H" in names(repo(df), " U15 ", "male")


def test_event_config_invalid_gender_is_rejected_explicitly():
    df = event_config_df()
    df.loc[0, "GENDER"] = "MALE_BAD"
    with pytest.raises(PilotConfigError, match="invalid GENDER='MALE_BAD'"):
        repo(df).event_config_rows(COMP_ID)


def test_event_config_duplicate_detection_is_case_insensitive_for_event_and_keys():
    df = event_config_df()
    duplicate = df.iloc[0].copy()
    duplicate["COMPETITION_ID"] = "acm5_2026"
    duplicate["DIVISION"] = "u15"
    duplicate["GENDER"] = "MALE"
    duplicate["EVENT"] = "100M"
    df.loc[len(df)] = duplicate
    with pytest.raises(PilotConfigError, match="duplicates"):
        repo(df).event_config_rows(COMP_ID)


def test_master_division_codes_cannot_collide_case_insensitively():
    r = PilotConfigRepository("test-url")
    divs = divisions_df()
    divs.loc[len(divs)] = {
        "DIVISION_CODE": "Open",
        "DIVISION_NAME": "Duplicate Open",
        "MIN_AGE": "16",
        "MAX_AGE": "",
        "DISPLAY_ORDER": 999,
        "ACTIVE": "TRUE",
    }
    tables = {
        "COMPETITIONS": competitions_df(),
        "DIVISIONS": divs,
        "EVENT_CONFIG": event_config_df(),
    }
    r.table = lambda worksheet, fresh=False: tables[worksheet].copy()
    with pytest.raises(PilotConfigError, match="case-insensitive"):
        r.event_config_rows(COMP_ID)


def test_inactive_special_division_master_rows_validate_but_are_not_age_eligible():
    r = repo()
    # Referential integrity accepts the codes because they exist in DIVISIONS.
    assert r.event_config_rows(COMP_ID) is not None
    # Their inactive master rows and blank age rules keep them out of registration
    # until SAA supplies explicit eligibility rules.
    assert r.division_rows(
        COMP_ID,
        gender="Female",
        birth_date="2000-06-15",
        competition_start_at="2026-10-22",
    ) == [{"code": "OPEN", "label": "Open", "age": 26}]


def test_competition_without_event_config_rows_is_configuration_error():
    r = repo()
    with pytest.raises(
        PilotConfigError,
        match="EVENT_CONFIG has no rows for COMPETITION_ID='NO_EVENT_CONFIG'",
    ):
        r.competition_event_rows("NO_EVENT_CONFIG")



def test_fee_lookup_configuration_keys_are_case_insensitive():
    r = PilotConfigRepository("test-url")
    fees = pd.DataFrame([{
        "COMPETITION_ID": "ACM5_2026",
        "ORGANIZATION_TYPE": "AFFILIATE",
        "REGISTRATION_PERIOD": "NORMAL",
        "FEE_PER_ENTRY_SGD": "12",
        "ACTIVE": "TRUE",
    }])
    r.table = lambda worksheet, fresh=False: fees.copy()
    assert str(r.fee_for(" acm5_2026 ", "affiliate", " normal ")) == "12"


def test_organization_id_lookup_is_case_insensitive():
    r = PilotConfigRepository("test-url")
    organizations = pd.DataFrame([{
        "ORGANIZATION_ID": "ORG_TEST_AFF_001",
        "ORGANIZATION_NAME": "Test Athletics Club",
        "TEAM_CODE": "TAC",
        "ORGANIZATION_TYPE": "AFFILIATE",
        "ACTIVE": "TRUE",
    }])
    r.table = lambda worksheet, fresh=False: organizations.copy()
    org = r.get_organization(" org_test_aff_001 ")
    assert org.organization_id == "ORG_TEST_AFF_001"
    assert org.organization_type == "AFFILIATE"


def test_event_config_is_enforced_server_side_if_ui_is_bypassed():
    r = repo()
    result = validate_athlete_selection(
        competition_id=COMP_ID,
        competition_start_at="2026-10-10",
        athlete_name="Test Athlete",
        birth_date="2011-01-01",
        gender="Female",
        division_code="U15",
        events=[{"event_name": "110m H", "event_code": "110m H"}],
        division_rows=divisions_df(),
        competition_event_rows=r.competition_event_rows(COMP_ID),
    )
    assert result.blocked
    assert result.codes == {"EVENT_INELIGIBLE"}
