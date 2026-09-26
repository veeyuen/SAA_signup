from __future__ import annotations

import pandas as pd
import pytest

from signup.results_reconciliation import (
    CANONICAL_RESULT_COLUMNS,
    ResultsSchemaError,
    normalise_dob,
    reconcile_results_to_registrations,
    validate_canonical_results,
)


def _result(**overrides):
    row = {column: "" for column in CANONICAL_RESULT_COLUMNS}
    row.update(
        {
            "FIRST_NAME": "Gabriel",
            "LAST_NAME": "Lee",
            "NAME": "Lee Jing Yi Gabriel",
            "RESULT": "13.20",
            "EVENT": "Triple Jump",
            "DIVISION": "Open",
            "GENDER": "Male",
            "UNIQUE_ID": "G897C03",
            "YEAR": "2026",
            "DATE": "2026-09-26",
            "COMPETITION": "Test Championships 2026",
            "DOB": "23/02/2003",
            "SOURCE": "Meet Manager",
            "INDOOR": False,
        }
    )
    row.update(overrides)
    return row


def _registration(**overrides):
    row = {
        "REGISTRATION_ID": "REG-001",
        "ORDER_ID": "ORD-001",
        "COMPETITION_ID": "COMP-001",
        "ATHLETE_ID": "G897C03",
        "ATHLETE_NAME": "Lee Jing Yi Gabriel",
        "DOB": "2003-02-23",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
        "FIRST_NAME": "Gabriel",
        "OTHER_NAME": "Jing Yi",
        "LAST_NAME": "Lee",
        "DIVISION": "Open",
    }
    row.update(overrides)
    return row


def _entry(**overrides):
    row = {
        "ENTRY_ID": "ENT-001",
        "REGISTRATION_ID": "REG-001",
        "ORDER_ID": "ORD-001",
        "COMPETITION_ID": "COMP-001",
        "ATHLETE_ID": "G897C03",
        "ATHLETE_NAME": "Lee Jing Yi Gabriel",
        "DOB": "2003-02-23",
        "EVENT_NAME": "Triple Jump",
        "EVENT_CODE": "TJ",
        "DIVISION": "Open",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
    }
    row.update(overrides)
    return row


def _reconcile(result_rows, registrations=None, entries=None):
    df = pd.DataFrame(result_rows)
    return reconcile_results_to_registrations(
        df,
        registrations if registrations is not None else [_registration()],
        entries if entries is not None else [_entry()],
        competition_id="COMP-001",
        competition_name="Test Championships 2026",
    ).rows


def test_current_schema_has_41_required_columns_and_indoor_is_only_addition():
    assert len(CANONICAL_RESULT_COLUMNS) == 41
    assert CANONICAL_RESULT_COLUMNS[-1] == "INDOOR"


def test_legacy_40_column_file_requires_explicit_indoor_value():
    df = pd.DataFrame([_result()]).drop(columns=["INDOOR"])

    with pytest.raises(ResultsSchemaError, match="INDOOR"):
        validate_canonical_results(df)

    upgraded, validation = validate_canonical_results(df, indoor_default=False)
    assert len(upgraded.columns) == 41
    assert upgraded.loc[0, "INDOOR"] == False
    assert validation.used_legacy_indoor_default is True
    assert validation.indoor_value_if_defaulted is False


def test_indoor_boolean_text_is_normalised_and_invalid_value_is_rejected():
    valid = pd.DataFrame([_result(INDOOR="TRUE"), _result(INDOOR="0")])
    normalized, _ = validate_canonical_results(valid)
    assert normalized["INDOOR"].tolist() == [True, False]

    invalid = pd.DataFrame([_result(INDOOR="sometimes")])
    with pytest.raises(ResultsSchemaError, match="non-boolean"):
        validate_canonical_results(invalid)


def test_required_column_order_is_not_enforced_and_extra_columns_are_preserved():
    df = pd.DataFrame([_result()])
    df = df[list(reversed(df.columns))]
    df["EXTRA_SOURCE_NOTE"] = "keep me"

    normalized, validation = validate_canonical_results(df)
    assert "EXTRA_SOURCE_NOTE" in normalized.columns
    assert validation.extra_columns == ("EXTRA_SOURCE_NOTE",)


def test_normalise_dob_supports_notebook_and_registration_formats():
    assert normalise_dob("23/02/2003") == "2003-02-23"
    assert normalise_dob("2003-02-23") == "2003-02-23"
    assert normalise_dob("not-a-date") == ""


def test_exact_unique_id_dob_name_and_event_match_is_automatic():
    report = _reconcile([_result()])
    row = report.iloc[0]

    assert row["MATCH_STATUS"] == "MATCHED"
    assert row["MATCH_REASON"] == "EXACT_IDENTITY_AND_EVENT_MATCH"
    assert row["REGISTRATION_ID"] == "REG-001"
    assert row["ENTRY_ID"] == "ENT-001"
    assert row["ORDER_ID"] == "ORD-001"


def test_name_case_and_repeated_whitespace_do_not_create_false_variation():
    report = _reconcile([_result(NAME="  lee   jing yi GABRIEL  ")])
    assert report.iloc[0]["MATCH_STATUS"] == "MATCHED"


def test_token_order_or_punctuation_name_variation_is_not_fuzzy_matched():
    reordered = _reconcile([_result(NAME="Gabriel Lee Jing Yi")])
    assert reordered.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert reordered.iloc[0]["MATCH_REASON"] == "NAME_MISMATCH"

    punctuation = _reconcile([_result(NAME="Lee, Jing Yi Gabriel")])
    assert punctuation.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert punctuation.iloc[0]["MATCH_REASON"] == "NAME_MISMATCH"


def test_uid_match_with_dob_mismatch_requires_review():
    report = _reconcile([_result(DOB="24/02/2003")])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "DOB_MISMATCH"


def test_uid_and_dob_match_with_name_mismatch_requires_review():
    report = _reconcile([_result(NAME="Lee Jing Yi Gabe")])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "NAME_MISMATCH"


def test_missing_identity_field_requires_review_not_name_only_matching():
    report = _reconcile([_result(UNIQUE_ID="")])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "MISSING_IDENTITY_FIELDS"


def test_unknown_unique_id_is_unmatched():
    report = _reconcile([_result(UNIQUE_ID="NO-SUCH-ID")])
    assert report.iloc[0]["MATCH_STATUS"] == "UNMATCHED"
    assert report.iloc[0]["MATCH_REASON"] == "NO_REGISTRATION_FOR_UNIQUE_ID"


def test_withdrawn_or_deleted_registration_is_not_automatically_matched():
    withdrawn = _registration(STATUS="WITHDRAWN", IS_DELETED="TRUE")
    report = _reconcile([_result()], registrations=[withdrawn])
    assert report.iloc[0]["MATCH_STATUS"] == "UNMATCHED"
    assert report.iloc[0]["MATCH_REASON"] == "NO_ACTIVE_REGISTRATION"


def test_exact_identity_but_unregistered_event_requires_review():
    report = _reconcile([_result(EVENT="Long Jump")])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "UNREGISTERED_EVENT"
    assert report.iloc[0]["REGISTRATION_ID"] == "REG-001"


def test_exact_identity_with_withdrawn_matching_event_requires_review():
    withdrawn_entry = _entry(STATUS="WITHDRAWN", IS_DELETED="TRUE")
    report = _reconcile([_result()], entries=[withdrawn_entry])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "NO_ACTIVE_EVENT_ENTRY"


def test_duplicate_exact_identity_is_ambiguous_not_auto_matched():
    second = _registration(REGISTRATION_ID="REG-002", ORDER_ID="ORD-002")
    second_entry = _entry(
        ENTRY_ID="ENT-002", REGISTRATION_ID="REG-002", ORDER_ID="ORD-002"
    )
    report = _reconcile(
        [_result()],
        registrations=[_registration(), second],
        entries=[_entry(), second_entry],
    )
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "AMBIGUOUS_IDENTITY"


def test_duplicate_active_event_entries_are_ambiguous_not_auto_matched():
    duplicate = _entry(ENTRY_ID="ENT-002")
    report = _reconcile([_result()], entries=[_entry(), duplicate])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "AMBIGUOUS_EVENT_ENTRY"


def test_wrong_competition_file_is_not_reconciled_to_selected_competition():
    report = _reconcile([_result(COMPETITION="Another Meet")])
    assert report.iloc[0]["MATCH_STATUS"] == "REVIEW"
    assert report.iloc[0]["MATCH_REASON"] == "COMPETITION_MISMATCH"


def test_structured_name_is_used_only_when_registration_athlete_name_is_blank():
    registration = _registration(
        ATHLETE_NAME="",
        FIRST_NAME="Gabriel",
        OTHER_NAME="Jing Yi",
        LAST_NAME="Lee",
    )
    result = _result(NAME="Gabriel Jing Yi Lee")
    report = _reconcile([result], registrations=[registration])
    assert report.iloc[0]["MATCH_STATUS"] == "MATCHED"


def test_reconciliation_preserves_original_result_columns_and_adds_fingerprint():
    report = _reconcile([_result(RESULT="13.37", INDOOR=True)])
    row = report.iloc[0]
    assert row["RESULT"] == "13.37"
    assert row["INDOOR"] == True
    assert isinstance(row["RESULT_FINGERPRINT"], str)
    assert len(row["RESULT_FINGERPRINT"]) == 64
