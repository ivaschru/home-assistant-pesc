"""Tests for PESC parsing and pre-submission safety checks."""

from datetime import date

import pytest

from custom_components.pesc.api import (
    PescApiClient,
    PescValidationError,
    normalize_login,
)
from custom_components.pesc.models import (
    PescAccount,
    PescMeter,
    PescMeterScale,
    PescReadingPeriod,
    PescSnapshot,
)


def _snapshot(*, digits_right: int = 0) -> PescSnapshot:
    meter = PescMeter(
        registration="registration-1",
        provider_id=1,
        serial="serial-1",
        name="Electric meter",
        status="ACTIVE",
        digits_left=6,
        digits_right=digits_right,
        scales=(
            PescMeterScale(10, "Day", 100.0, date(2026, 6, 20), "kWh"),
            PescMeterScale(20, "Night", 50.0, date(2026, 6, 20), "kWh"),
        ),
    )
    account = PescAccount(
        account_id=123,
        alias="Test object",
        account_number="synthetic",
        provider_code="PSK",
        auto_payment_on=False,
        delivery=None,
        reading_state="AVAILABLE",
        reading_period=PescReadingPeriod(
            "July", date(2026, 7, 1), date(2026, 7, 31), 25, False, None
        ),
        balance=0,
        amount_due=0,
        overpayment=0,
        current_bill_amount=0,
        current_bill_timestamp=None,
        meters=(meter,),
    )
    return PescSnapshot(accounts={account.account_id: account})


class RecordingClient(PescApiClient):
    """Minimal client double that records the final write request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []

    async def _request(self, method: str, path: str, *, payload=None):
        self.requests.append((method, path, payload))
        return {}


def test_phone_normalization() -> None:
    """Match PESC's required international Russian phone format."""

    assert normalize_login("8 (999) 123-45-67") == ("+79991234567", "PHONE")


def test_all_scales_are_required() -> None:
    """Never send a partial multi-tariff reading by accident."""

    meter = _snapshot().accounts[123].meters[0]
    with pytest.raises(PescValidationError, match="every meter scale"):
        PescApiClient._scale_values(meter, {"Day": 101})


def test_fraction_rejected_for_integer_meter() -> None:
    """Do not silently round values when PESC declares integer precision."""

    meter = _snapshot().accounts[123].meters[0]
    with pytest.raises(PescValidationError, match="precision"):
        PescApiClient._scale_values(meter, {"Day": 101.2, "Night": 51})


def test_lower_reading_is_rejected() -> None:
    """Protect against replacing a cumulative reading with a smaller value."""

    meter = _snapshot().accounts[123].meters[0]
    with pytest.raises(PescValidationError, match="lower"):
        PescApiClient._scale_values(meter, {"Day": 99, "Night": 51})


async def test_submission_uses_discovered_scale_ids() -> None:
    """Build the same endpoint and scale payload as the official web client."""

    client = RecordingClient()
    result = await client.async_submit_readings(
        123,
        None,
        {"day": 101, "20": 51},
        snapshot=_snapshot(),
    )
    assert result.accepted is True
    assert client.requests == [
        (
            "POST",
            "v8/accounts/123/meters/registration-1/reading",
            [{"scaleId": 10, "value": 101}, {"scaleId": 20, "value": 51}],
        )
    ]
