import pandas as pd
import pytest

from signup.pilot_config import PilotConfigError, PilotConfigRepository
from signup.competition_rules import validate_athlete_selection


COMP_ID = "ACM5_2026"


def competitions_df():
    return pd.DataFrame([
        {"COMPETITION_ID": COMP_ID},
        {"COMPETITION_ID": "LEGACY"},
    ])


def divisions_df():
    return pd.DataFrame([
        {"DIVISION_CODE": "U15", "DIVISION_NAME": "U15", "MIN_AGE": "13", "MAX_AGE": "15", "DISPLAY_ORDER": 10, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U18", "DIVISION_NAME": "U18", "MIN_AGE": "16", "MAX_AGE": "17", "DISPLAY_ORDER": 20, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U20", "DIVISION_NAME": "U20", "MIN_AGE": "18", "MAX_AGE": "19", "DISPLAY_ORDER": 30, "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "Open", "DIVISION_NAME": "Open", "MIN_AGE": "16", "MAX_AGE": "", "DISPLAY_ORDER": 40, "ACTIVE": "TRUE"},
    ])


def legacy_df():
    return pd.DataFrame([
        {"COMPETITION_ID": "LEGACY", "GENDER": "M", "DIVISION_CODE": "U15", "EVENT_CODE": "100", "EVENT_NAME": "100m", "ACTIVE": "TRUE"}
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
    add("Open", "Male", "10000m Race Walk", "110m H")
    add("Open", "Female", "10000m Race Walk", "100m H")
    add("Novice", "Any", "High Jump", "Pole Vault")
    add("Intermediate", "Any", "High Jump", "Pole Vault")
    add("Advance", "Any", "High Jump", "Pole Vault")
    return pd.DataFrame(rows)


def repo(event_config=None):
    r = PilotConfigRepository("test-url")
    tables = {
        "COMPETITIONS": competitions_df(),
        "DIVISIONS": divisions_df(),
        "COMPETITION_EVENTS": legacy_df(),
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
    assert {"10000m Race Walk", "110m H"}.issubset(names(r, "Open", "Male"))
    assert {"10000m Race Walk", "100m H"}.issubset(names(r, "Open", "Female"))


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


def test_legacy_competition_without_event_config_rows_uses_existing_table():
    r = repo()
    assert r.event_config_rows("LEGACY") is None
    assert r.event_options("LEGACY", "Male", "U15") == [("100m", "100")]


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
