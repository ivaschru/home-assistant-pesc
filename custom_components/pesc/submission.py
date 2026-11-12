"""Optional error responses for automations that must explain failed readings.

The normal service continues to raise on failure.  An automation can explicitly
request a structured response and branch on ``accepted`` instead of losing the
provider message when Home Assistant's ``continue_on_error`` catches an exception.
The separate explicit confirmation flag can accept one known legal advisory;
this wrapper itself never retries a final write.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .api import (
    PescApiClient,
    PescApiError,
    PescAuthenticationError,
    PescConnectionError,
    PescHighConsumptionWarning,
    PescValidationError,
)
from .models import PescSnapshot


async def async_submit_readings_response(
    client: PescApiClient,
    account_id: int,
    meter: str | None,
    readings: Mapping[str, Any],
    *,
    snapshot: PescSnapshot | None,
    return_errors: bool = False,
    confirm_high_consumption: bool = False,
    expected_period: str | None = None,
) -> dict[str, Any]:
    """Return a success or an explicitly requested, credential-free failure.

    Only known PESC failures are converted; programming errors still propagate.
    A transport failure is not proof that the provider rejected or accepted the
    readings.  Preserve that distinction in ``error_kind`` and leave readback to
    the existing REGISTERED guard before any later scheduled submission.
    """

    try:
        result = await client.async_submit_readings(
            account_id,
            meter,
            readings,
            snapshot=snapshot,
            confirm_high_consumption=confirm_high_consumption,
            expected_period=expected_period,
        )
    except PescApiError as error:
        if not return_errors:
            raise
        if isinstance(error, PescValidationError):
            kind = "validation"
        elif isinstance(error, PescAuthenticationError):
            kind = "authentication"
        elif isinstance(error, PescConnectionError):
            kind = "connection"
        elif error.http_status is not None and 400 <= error.http_status < 500:
            kind = "provider_rejection"
        else:
            kind = "provider"
        return {
            "accepted": False,
            "error": str(error),
            "error_code": error.code,
            "error_kind": kind,
            "http_status": error.http_status,
            "confirmation_required": isinstance(error, PescHighConsumptionWarning),
        }
    return asdict(result)
