"""Phase 5B waiver/compliance helpers.

The clarified requirements establish only the following waiver rules:
- the registering representative signs for all athletes in the submission;
- the waiver applies to one competition;
- a new waiver is required for every registration/submission.

The legal waiver wording itself is deliberately not defined here. The UI may display
configured wording when SAA supplies it, but this module only validates and records the
acknowledgement metadata.
"""

from __future__ import annotations

from typing import Any


class WaiverComplianceError(ValueError):
    """Raised when a waiver acknowledgement is incomplete or inconsistent."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalise_signer_name(value: Any) -> str:
    """Normalise a typed representative name without changing its substance."""
    return " ".join(_clean(value).split())


def validate_waiver_acknowledgement(
    *,
    accepted: bool,
    signer_name: Any,
    waiver_version: Any,
) -> tuple[str, str]:
    """Validate the explicit order-level waiver acknowledgement.

    Returns the normalised signer name and waiver version for persistence.
    """
    if not accepted:
        raise WaiverComplianceError(
            "The competition waiver must be acknowledged before the order is submitted."
        )

    signer = normalise_signer_name(signer_name)
    if not signer:
        raise WaiverComplianceError(
            "Enter the representative name for the waiver acknowledgement."
        )

    version = _clean(waiver_version)
    if not version:
        raise WaiverComplianceError(
            "The waiver version is not configured. Contact SA Events before submitting."
        )

    return signer, version


def build_waiver_record(
    *,
    order_id: Any,
    competition_id: Any,
    organization_id: Any,
    signed_by_user_id: Any,
    signed_by_name: Any,
    waiver_version: Any,
    signed_at: Any,
    accepted: bool,
) -> dict[str, str]:
    """Build one immutable waiver record for one order/competition submission.

    The WAIVER_ID is derived from ORDER_ID. Therefore a new order/submission naturally
    creates a new waiver, while a retry of the same idempotent order reuses the same waiver.
    """
    order = _clean(order_id)
    competition = _clean(competition_id)
    organization = _clean(organization_id)
    user_id = _clean(signed_by_user_id)
    timestamp = _clean(signed_at)

    if not order:
        raise WaiverComplianceError("ORDER_ID is required for the waiver record.")
    if not competition:
        raise WaiverComplianceError("COMPETITION_ID is required for the waiver record.")
    if not organization:
        raise WaiverComplianceError("ORGANIZATION_ID is required for the waiver record.")
    if not user_id:
        raise WaiverComplianceError("SIGNED_BY_USER_ID is required for the waiver record.")
    if not timestamp:
        raise WaiverComplianceError("SIGNED_AT is required for the waiver record.")

    signer, version = validate_waiver_acknowledgement(
        accepted=accepted,
        signer_name=signed_by_name,
        waiver_version=waiver_version,
    )

    waiver_suffix = order.removeprefix("ORD-")
    return {
        "WAIVER_ID": f"WVR-{waiver_suffix}",
        "ORDER_ID": order,
        "COMPETITION_ID": competition,
        "ORGANIZATION_ID": organization,
        "SIGNED_BY_USER_ID": user_id,
        "SIGNED_BY_NAME": signer,
        "WAIVER_VERSION": version,
        "SIGNED_AT": timestamp,
    }
