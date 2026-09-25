from signup.competition_rules import (
    age_on_date,
    validate_athlete_selection,
    validate_cart_competition_rules,
)


def divisions():
    return [
        {"DIVISION_CODE": "U9", "MIN_AGE": "7", "MAX_AGE": "8", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U11", "MIN_AGE": "9", "MAX_AGE": "10", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U13", "MIN_AGE": "11", "MAX_AGE": "12", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U15", "MIN_AGE": "13", "MAX_AGE": "15", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U18", "MIN_AGE": "16", "MAX_AGE": "17", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "U20", "MIN_AGE": "18", "MAX_AGE": "19", "ACTIVE": "TRUE"},
        {"DIVISION_CODE": "Open", "MIN_AGE": "16", "MAX_AGE": "", "ACTIVE": "TRUE"},
    ]


def competitions(start="2026-10-10", active="TRUE", status="OPEN"):
    return [
        {
            "COMPETITION_ID": "COMP1",
            "COMPETITION_START_AT": start,
            "STATUS": status,
            "ACTIVE": active,
        }
    ]


def event_rows(*, include_u18=True, active_100=True):
    rows = [
        {
            "COMPETITION_ID": "COMP1",
            "GENDER": "M",
            "DIVISION_CODE": "U15",
            "EVENT_CODE": "100",
            "EVENT_NAME": "100m",
            "ACTIVE": "TRUE" if active_100 else "FALSE",
        },
        {
            "COMPETITION_ID": "COMP1",
            "GENDER": "M",
            "DIVISION_CODE": "U20",
            "EVENT_CODE": "100",
            "EVENT_NAME": "100m",
            "ACTIVE": "TRUE",
        },
        {
            "COMPETITION_ID": "COMP1",
            "GENDER": "M",
            "DIVISION_CODE": "Open",
            "EVENT_CODE": "100",
            "EVENT_NAME": "100m",
            "ACTIVE": "TRUE",
        },
        {
            "COMPETITION_ID": "COMP1",
            "GENDER": "F",
            "DIVISION_CODE": "U18",
            "EVENT_CODE": "100",
            "EVENT_NAME": "100m",
            "ACTIVE": "TRUE",
        },
        {
            "COMPETITION_ID": "COMP1",
            "GENDER": "M",
            "DIVISION_CODE": "U15",
            "EVENT_CODE": "HJ",
            "EVENT_NAME": "High Jump",
            "ACTIVE": "TRUE",
        },
    ]
    if include_u18:
        rows.append(
            {
                "COMPETITION_ID": "COMP1",
                "GENDER": "M",
                "DIVISION_CODE": "U18",
                "EVENT_CODE": "100",
                "EVENT_NAME": "100m",
                "ACTIVE": "TRUE",
            }
        )
    return rows


def validate(*, birth_date, division, gender="Male", events=None, div_rows=None, ev_rows=None):
    return validate_athlete_selection(
        competition_id="COMP1",
        competition_start_at="2026-10-10",
        athlete_name="Test Athlete",
        birth_date=birth_date,
        gender=gender,
        division_code=division,
        events=events or [{"event_name": "100m", "event_code": "100"}],
        division_rows=div_rows or divisions(),
        competition_event_rows=ev_rows or event_rows(),
    )


def cart_item(*, birth_date="2010-10-10", division="U18", gender="Male", event="100m", code="100"):
    return {
        "registration_id": "REG1",
        "athlete_name": "Test Athlete",
        "division": division,
        "events": [event],
        "entry_rows": [
            {
                "competition_id": "COMP1",
                "birth_date": birth_date,
                "gender": gender,
                "event_division": division,
                "event": event,
                "event_code": code,
            }
        ],
    }


def test_age_on_date_uses_exact_birthday_boundary():
    assert age_on_date("2010-10-10", "2026-10-10") == 16
    assert age_on_date("2010-10-11", "2026-10-10") == 15


def test_age_19_is_eligible_for_u20_and_open():
    u20 = validate(birth_date="2007-10-10", division="U20")
    open_result = validate(birth_date="2007-10-10", division="Open")
    assert not u20.blocked
    assert not open_result.blocked


def test_age_15_is_eligible_for_u15_but_not_u18():
    u15 = validate(birth_date="2010-10-11", division="U15")
    u18 = validate(birth_date="2010-10-11", division="U18")
    assert not u15.blocked
    assert u18.blocked
    assert u18.codes == {"DIVISION_INELIGIBLE"}


def test_turning_16_on_competition_start_date_allows_u18():
    result = validate(birth_date="2010-10-10", division="U18")
    assert not result.blocked


def test_turning_16_day_after_competition_remains_u15():
    result = validate(birth_date="2010-10-11", division="U15")
    assert not result.blocked


def test_age_valid_division_without_configured_event_is_blocked():
    result = validate(
        birth_date="2010-10-10",
        division="U18",
        ev_rows=event_rows(include_u18=False),
    )
    assert result.blocked
    assert result.codes == {"EVENT_INELIGIBLE"}


def test_event_configured_for_other_gender_is_blocked():
    result = validate(
        birth_date="2010-10-10",
        division="U18",
        gender="Male",
        ev_rows=[
            {
                "COMPETITION_ID": "COMP1",
                "GENDER": "F",
                "DIVISION_CODE": "U18",
                "EVENT_CODE": "100",
                "EVENT_NAME": "100m",
                "ACTIVE": "TRUE",
            }
        ],
    )
    assert result.blocked
    assert result.codes == {"EVENT_INELIGIBLE"}


def test_inactive_event_is_blocked():
    result = validate(
        birth_date="2010-10-11",
        division="U15",
        ev_rows=event_rows(active_100=False),
    )
    assert result.blocked
    assert result.codes == {"EVENT_INELIGIBLE"}


def test_missing_dob_fails_closed():
    result = validate(birth_date="", division="U18")
    assert result.blocked
    assert result.codes == {"DIVISION_INELIGIBLE"}


def test_invalid_division_age_configuration_fails_closed():
    bad_divisions = divisions()
    bad_divisions[4] = {
        "DIVISION_CODE": "U18",
        "MIN_AGE": "sixteen",
        "MAX_AGE": "17",
        "ACTIVE": "TRUE",
    }
    result = validate(
        birth_date="2010-10-10",
        division="U18",
        div_rows=bad_divisions,
    )
    assert result.blocked
    assert result.codes == {"CONFIG_ERROR"}


def test_missing_competition_start_date_fails_closed():
    result = validate_cart_competition_rules(
        [cart_item()],
        competition_id="COMP1",
        competition_rows=competitions(start=""),
        division_rows=divisions(),
        competition_event_rows=event_rows(),
    )
    assert result.blocked
    assert result.codes == {"CONFIG_ERROR"}


def test_inactive_competition_is_blocked():
    result = validate_cart_competition_rules(
        [cart_item()],
        competition_id="COMP1",
        competition_rows=competitions(active="FALSE"),
        division_rows=divisions(),
        competition_event_rows=event_rows(),
    )
    assert result.blocked
    assert result.codes == {"COMPETITION_UNAVAILABLE"}


def test_stale_cart_is_blocked_when_event_removed_from_current_config():
    original = validate_cart_competition_rules(
        [cart_item()],
        competition_id="COMP1",
        competition_rows=competitions(),
        division_rows=divisions(),
        competition_event_rows=event_rows(),
    )
    changed_rows = [
        row
        for row in event_rows()
        if not (
            row["GENDER"] == "M"
            and row["DIVISION_CODE"] == "U18"
            and row["EVENT_CODE"] == "100"
        )
    ]
    changed = validate_cart_competition_rules(
        [cart_item()],
        competition_id="COMP1",
        competition_rows=competitions(),
        division_rows=divisions(),
        competition_event_rows=changed_rows,
    )
    assert not original.blocked
    assert changed.blocked
    assert changed.codes == {"EVENT_INELIGIBLE"}


def test_event_code_remains_authoritative_if_display_name_changes():
    renamed_rows = event_rows()
    for row in renamed_rows:
        if row["GENDER"] == "M" and row["DIVISION_CODE"] == "U18" and row["EVENT_CODE"] == "100":
            row["EVENT_NAME"] = "100 Metres"
    result = validate_cart_competition_rules(
        [cart_item(event="100m", code="100")],
        competition_id="COMP1",
        competition_rows=competitions(),
        division_rows=divisions(),
        competition_event_rows=renamed_rows,
    )
    assert not result.blocked


def test_cart_competition_mismatch_fails_closed():
    item = cart_item()
    item["entry_rows"][0]["competition_id"] = "OTHER_COMP"
    result = validate_cart_competition_rules(
        [item],
        competition_id="COMP1",
        competition_rows=competitions(),
        division_rows=divisions(),
        competition_event_rows=event_rows(),
    )
    assert result.blocked
    assert result.codes == {"CONFIG_ERROR"}
