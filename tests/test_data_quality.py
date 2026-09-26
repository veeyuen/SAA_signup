from signup.data_quality import latest_review_state, scan_data_quality


def reg(**updates):
    row = {
        "REGISTRATION_ID": "REG-1",
        "ORDER_ID": "ORD-1",
        "COMPETITION_ID": "COMP-1",
        "ATHLETE_ID": "ATH-1",
        "ORGANIZATION_ID": "ORG-1",
        "DIVISION": "U18",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
        "ATHLETE_NAME": "Test Athlete",
        "DOB": "2010-01-01",
        "GENDER": "Male",
        "TEAM_CODE": "T1",
        "TEAM_NAME": "Team 1",
    }
    row.update(updates)
    return row


def entry(**updates):
    row = {
        "ENTRY_ID": "ENT-1",
        "REGISTRATION_ID": "REG-1",
        "ORDER_ID": "ORD-1",
        "COMPETITION_ID": "COMP-1",
        "ATHLETE_ID": "ATH-1",
        "ORGANIZATION_ID": "ORG-1",
        "EVENT_NAME": "100m",
        "EVENT_CODE": "100",
        "DIVISION": "U18",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
        "ATHLETE_NAME": "Test Athlete",
        "DOB": "2010-01-01",
        "GENDER": "Male",
        "TEAM_CODE": "T1",
        "TEAM_NAME": "Team 1",
    }
    row.update(updates)
    return row


def order(order_id="ORD-1"):
    return {"ORDER_ID": order_id, "COMPETITION_ID": "COMP-1", "STATUS": "CONFIRMED"}


def types(issues):
    return {issue.issue_type for issue in issues}


def test_clean_dataset_has_no_issues():
    issues = scan_data_quality(orders=[order()], registrations=[reg()], entries=[entry()])
    assert issues == []


def test_identity_collision_same_name_dob_different_ids():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ATHLETE_ID="ATH-2")],
        entries=[],
    )
    assert "IDENTITY_COLLISION" in types(issues)


def test_name_normalisation_is_conservative_but_punctuation_insensitive():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[
            reg(ATHLETE_NAME="Tan, Kai Ming"),
            reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ATHLETE_ID="ATH-2", ATHLETE_NAME="Tan Kai Ming"),
        ],
        entries=[],
    )
    assert "IDENTITY_COLLISION" in types(issues)


def test_different_dob_does_not_raise_identity_collision():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ATHLETE_ID="ATH-2", DOB="2010-01-02")],
        entries=[],
    )
    assert "IDENTITY_COLLISION" not in types(issues)


def test_same_id_conflicting_dob_is_flagged():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", DOB="2011-01-01")],
        entries=[],
    )
    assert "ATHLETE_ID_CONFLICT" in types(issues)


def test_duplicate_active_event_is_flagged():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2")],
        entries=[entry(), entry(ENTRY_ID="ENT-2", REGISTRATION_ID="REG-2", ORDER_ID="ORD-2")],
    )
    assert "DUPLICATE_ACTIVE_EVENT" in types(issues)


def test_withdrawn_duplicate_event_is_ignored():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2")],
        entries=[entry(), entry(ENTRY_ID="ENT-2", REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", STATUS="WITHDRAWN", IS_DELETED="TRUE")],
    )
    assert "DUPLICATE_ACTIVE_EVENT" not in types(issues)


def test_team_conflict_is_flagged():
    issues = scan_data_quality(
        orders=[order(), order("ORD-2")],
        registrations=[reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ORGANIZATION_ID="ORG-2")],
        entries=[entry(), entry(ENTRY_ID="ENT-2", REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ORGANIZATION_ID="ORG-2", TEAM_CODE="T2", TEAM_NAME="Team 2")],
    )
    assert "TEAM_CONFLICT" in types(issues)


def test_different_events_same_team_are_not_team_conflict():
    issues = scan_data_quality(
        orders=[order()],
        registrations=[reg()],
        entries=[entry(), entry(ENTRY_ID="ENT-2", EVENT_NAME="200m", EVENT_CODE="200")],
    )
    assert "TEAM_CONFLICT" not in types(issues)
    assert "DUPLICATE_ACTIVE_EVENT" not in types(issues)


def test_orphan_registration_is_flagged():
    issues = scan_data_quality(orders=[], registrations=[reg()], entries=[])
    assert "ORPHAN_REGISTRATION" in types(issues)


def test_orphan_event_entry_is_flagged():
    issues = scan_data_quality(orders=[order()], registrations=[], entries=[entry()])
    assert "ORPHAN_EVENT_ENTRY" in types(issues)


def test_entry_registration_mismatch_is_flagged():
    issues = scan_data_quality(
        orders=[order()],
        registrations=[reg()],
        entries=[entry(TEAM_NAME="Different Team")],
    )
    assert "ENTRY_REGISTRATION_MISMATCH" in types(issues)


def test_blank_on_one_side_does_not_create_projection_mismatch():
    issues = scan_data_quality(
        orders=[order()],
        registrations=[reg(TEAM_NAME="")],
        entries=[entry(TEAM_NAME="Team 1")],
    )
    assert "ENTRY_REGISTRATION_MISMATCH" not in types(issues)


def test_missing_registration_identity_data_is_flagged():
    issues = scan_data_quality(orders=[order()], registrations=[reg(GENDER="")], entries=[])
    assert "MISSING_REGISTRATION_DATA" in types(issues)


def test_invalid_dob_is_flagged():
    issues = scan_data_quality(orders=[order()], registrations=[reg(DOB="not-a-date")], entries=[])
    assert "MISSING_REGISTRATION_DATA" in types(issues)


def test_missing_entry_event_is_flagged():
    issues = scan_data_quality(orders=[order()], registrations=[reg()], entries=[entry(EVENT_NAME="")])
    assert "MISSING_ENTRY_DATA" in types(issues)


def test_issue_key_is_stable_across_input_order():
    regs = [reg(), reg(REGISTRATION_ID="REG-2", ORDER_ID="ORD-2", ATHLETE_ID="ATH-2")]
    a = scan_data_quality(orders=[order(), order("ORD-2")], registrations=regs, entries=[])
    b = scan_data_quality(orders=[order("ORD-2"), order()], registrations=list(reversed(regs)), entries=[])
    assert [(x.issue_type, x.issue_key) for x in a] == [(x.issue_type, x.issue_key) for x in b]


def test_latest_review_state_uses_latest_audit():
    states = latest_review_state([
        {
            "TIMESTAMP": "2026-09-01T00:00:00+00:00",
            "ENTITY_TYPE": "DATA_QUALITY_ISSUE",
            "ENTITY_ID": "DQ-1",
            "ACTION": "DATA_QUALITY_REVIEW_ACKNOWLEDGED",
            "REASON": "checking",
        },
        {
            "TIMESTAMP": "2026-09-02T00:00:00+00:00",
            "ENTITY_TYPE": "DATA_QUALITY_ISSUE",
            "ENTITY_ID": "DQ-1",
            "ACTION": "DATA_QUALITY_FALSE_POSITIVE",
            "REASON": "verified",
        },
    ])
    assert states["DQ-1"]["STATUS"] == "FALSE_POSITIVE"
    assert states["DQ-1"]["REASON"] == "verified"


def test_unrelated_audit_actions_are_ignored():
    assert latest_review_state([{
        "TIMESTAMP": "2026-09-02",
        "ENTITY_TYPE": "EVENT_ENTRY",
        "ENTITY_ID": "DQ-1",
        "ACTION": "EVENT_ENTRY_WITHDRAWN",
    }]) == {}


def test_equivalent_gender_and_dob_formats_do_not_create_projection_mismatch():
    issues = scan_data_quality(
        orders=[order()],
        registrations=[reg(GENDER="M", DOB="2010-01-01")],
        entries=[entry(GENDER="Male", DOB="2010-01-01T00:00:00")],
    )
    assert "ENTRY_REGISTRATION_MISMATCH" not in types(issues)
