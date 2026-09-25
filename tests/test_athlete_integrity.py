from signup.athlete_integrity import check_candidate_against_existing


def candidate(**overrides):
    base = {
        "competition_id": "COMP1",
        "organization_id": "ORG1",
        "team_code": "AAA",
        "athlete_id": "ATH1",
        "athlete_name": "Jane Doe",
        "dob": "2010-01-02",
        "events": [{"event_name": "100m", "event_code": "100"}],
    }
    base.update(overrides)
    return base


def existing(**overrides):
    base = {
        "ENTRY_ID": "ENT1",
        "REGISTRATION_ID": "REG1",
        "ORDER_ID": "ORD1",
        "COMPETITION_ID": "COMP1",
        "ORGANIZATION_ID": "ORG1",
        "TEAM_CODE": "AAA",
        "ATHLETE_ID": "ATH1",
        "ATHLETE_NAME": "Jane Doe",
        "DOB": "2010-01-02",
        "EVENT_NAME": "100m",
        "EVENT_CODE": "100",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
    }
    base.update(overrides)
    return base


def test_duplicate_event_is_blocked():
    result = check_candidate_against_existing(candidate(), [existing()])
    assert result.blocked
    assert result.codes == {"DUPLICATE_EVENT"}


def test_same_athlete_new_event_same_team_is_allowed():
    result = check_candidate_against_existing(
        candidate(events=[{"event_name": "200m", "event_code": "200"}]),
        [existing()],
    )
    assert not result.blocked


def test_same_athlete_different_team_is_blocked():
    result = check_candidate_against_existing(
        candidate(organization_id="ORG2", team_code="BBB"),
        [existing()],
    )
    assert result.blocked
    assert result.codes == {"TEAM_CONFLICT"}


def test_name_dob_fallback_with_missing_candidate_id():
    result = check_candidate_against_existing(
        candidate(athlete_id=""),
        [existing()],
    )
    assert result.blocked
    assert result.codes == {"DUPLICATE_EVENT"}


def test_different_ids_same_name_dob_is_identity_collision():
    result = check_candidate_against_existing(
        candidate(athlete_id="ATH2"),
        [existing(ATHLETE_ID="ATH1")],
    )
    assert result.blocked
    assert result.codes == {"IDENTITY_COLLISION"}


def test_withdrawn_entry_does_not_block():
    result = check_candidate_against_existing(
        candidate(),
        [existing(STATUS="WITHDRAWN", IS_DELETED="TRUE")],
    )
    assert not result.blocked


def test_different_competition_does_not_block():
    result = check_candidate_against_existing(candidate(competition_id="COMP2"), [existing()])
    assert not result.blocked


def test_one_duplicate_among_multiple_candidate_events_blocks():
    result = check_candidate_against_existing(
        candidate(
            events=[
                {"event_name": "100m", "event_code": "100"},
                {"event_name": "200m", "event_code": "200"},
            ]
        ),
        [existing()],
    )
    assert result.blocked
    assert result.codes == {"DUPLICATE_EVENT"}


def test_multiple_existing_events_same_other_team_yield_one_team_conflict():
    existing_rows = [
        existing(ENTRY_ID="ENT1", EVENT_NAME="100m", EVENT_CODE="100"),
        existing(
            ENTRY_ID="ENT2",
            REGISTRATION_ID="REG2",
            ORDER_ID="ORD2",
            EVENT_NAME="Long Jump",
            EVENT_CODE="LJ",
        ),
    ]
    result = check_candidate_against_existing(
        candidate(
            organization_id="ORG2",
            team_code="BBB",
            events=[{"event_name": "200m", "event_code": "200"}],
        ),
        existing_rows,
    )
    assert result.blocked
    assert result.codes == {"TEAM_CONFLICT"}
    assert len(result.conflicts) == 1
