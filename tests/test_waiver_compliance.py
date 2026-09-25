import pytest

from signup.waiver_compliance import (
    WaiverComplianceError,
    build_waiver_record,
    normalise_signer_name,
    validate_waiver_acknowledgement,
)


def _record(**overrides):
    values = {
        "order_id": "ORD-ABC123",
        "competition_id": "COMP_TEST_NORMAL",
        "organization_id": "ORG_TEST_AFF_001",
        "signed_by_user_id": "USR_TEST_AFF_001",
        "signed_by_name": "  Vee   Yuen  ",
        "waiver_version": "TEST_WAIVER_V1",
        "signed_at": "2026-09-25T11:00:00+00:00",
        "accepted": True,
    }
    values.update(overrides)
    return build_waiver_record(**values)


def test_normalise_signer_name_collapses_whitespace():
    assert normalise_signer_name("  Vee   Yuen  ") == "Vee Yuen"


def test_acknowledgement_requires_explicit_acceptance():
    with pytest.raises(WaiverComplianceError, match="must be acknowledged"):
        validate_waiver_acknowledgement(
            accepted=False,
            signer_name="Vee Yuen",
            waiver_version="TEST_WAIVER_V1",
        )


def test_acknowledgement_requires_representative_name():
    with pytest.raises(WaiverComplianceError, match="representative name"):
        validate_waiver_acknowledgement(
            accepted=True,
            signer_name="   ",
            waiver_version="TEST_WAIVER_V1",
        )


def test_acknowledgement_requires_configured_version():
    with pytest.raises(WaiverComplianceError, match="version is not configured"):
        validate_waiver_acknowledgement(
            accepted=True,
            signer_name="Vee Yuen",
            waiver_version="",
        )


def test_waiver_record_is_bound_to_one_order_and_competition():
    row = _record()
    assert row == {
        "WAIVER_ID": "WVR-ABC123",
        "ORDER_ID": "ORD-ABC123",
        "COMPETITION_ID": "COMP_TEST_NORMAL",
        "ORGANIZATION_ID": "ORG_TEST_AFF_001",
        "SIGNED_BY_USER_ID": "USR_TEST_AFF_001",
        "SIGNED_BY_NAME": "Vee Yuen",
        "WAIVER_VERSION": "TEST_WAIVER_V1",
        "SIGNED_AT": "2026-09-25T11:00:00+00:00",
    }


def test_new_order_creates_new_waiver_id():
    first = _record(order_id="ORD-FIRST")
    second = _record(order_id="ORD-SECOND")
    assert first["WAIVER_ID"] == "WVR-FIRST"
    assert second["WAIVER_ID"] == "WVR-SECOND"
    assert first["WAIVER_ID"] != second["WAIVER_ID"]


def test_retry_same_order_is_idempotent_for_waiver_identity():
    first = _record(order_id="ORD-RETRY")
    second = _record(order_id="ORD-RETRY")
    assert first["WAIVER_ID"] == second["WAIVER_ID"] == "WVR-RETRY"


def test_waiver_requires_competition_id():
    with pytest.raises(WaiverComplianceError, match="COMPETITION_ID"):
        _record(competition_id="")
