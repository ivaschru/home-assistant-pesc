"""Tests for PESC parsing and pre-submission safety checks."""

from datetime import date
from uuid import UUID

import pytest

from custom_components.pesc.api import (
    PescApiClient,
    PescValidationError,
    _parse_date,
    normalize_login,
)
from custom_components.pesc.const import ACCOUNT_TYPE_LEGAL
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


class LegalRecordingClient(RecordingClient):
    """Record the legal-account validation calls and final contract write."""

    def __init__(self) -> None:
        super().__init__()
        self._account_type = ACCOUNT_TYPE_LEGAL

    async def _request(self, method: str, path: str, *, payload=None):
        self.requests.append((method, path, payload))
        return "Показания успешно переданы" if path.endswith("/indications") else {}


def test_phone_normalization() -> None:
    """Match PESC's required international Russian phone format."""

    assert normalize_login("8 (999) 123-45-67") == ("+79991234567", "PHONE")


def test_legal_login_is_not_rewritten_as_phone() -> None:
    """Keep organization usernames intact for the legal cabinet."""

    assert normalize_login("contract-user", ACCOUNT_TYPE_LEGAL) == (
        "contract-user",
        "LOGIN",
    )


def test_legal_russian_reading_month_is_parsed() -> None:
    """Treat the legal cabinet's Russian month as a stable month marker."""

    assert _parse_date("июнь 2026") == date(2026, 6, 1)


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


async def test_legal_submission_validates_registers_and_sends_one_payload() -> None:
    """Mirror the legal frontend's per-scale validation and final v7 write."""

    meter = PescMeter(
        registration="2457293",
        provider_id=1,
        serial="synthetic-legal-meter",
        name="Parking meter",
        status="ACTIVE",
        digits_left=6,
        digits_right=2,
        scales=(
            PescMeterScale(
                1,
                "День",
                5315.78,
                date(2026, 6, 1),
                "A+",
                register_id="register-day",
                indication_id=101,
                upstream_name="DAY",
                time_of_use="День",
            ),
            PescMeterScale(
                2,
                "Ночь",
                12206.63,
                date(2026, 6, 1),
                "A+",
                register_id="register-night",
                indication_id=102,
                upstream_name="NIGHT",
                time_of_use="Ночь",
            ),
        ),
    )
    account = PescAccount(
        account_id=1001,
        alias="Synthetic legal object",
        account_number="contract-1",
        provider_code="PSK",
        auto_payment_on=False,
        delivery=None,
        reading_state="AVAILABLE",
        reading_period=PescReadingPeriod(
            "July", date(2026, 7, 17), date(2026, 8, 17), None, False, None
        ),
        balance=None,
        amount_due=None,
        overpayment=None,
        current_bill_amount=None,
        current_bill_timestamp=None,
        meters=(meter,),
        account_type=ACCOUNT_TYPE_LEGAL,
    )
    client = LegalRecordingClient()
    result = await client.async_submit_readings(
        account.account_id,
        meter.serial,
        {"День": 5316.25, "NIGHT": 12207.5},
        snapshot=PescSnapshot(accounts={account.account_id: account}),
    )

    assert result.accepted is True
    assert [request[1] for request in client.requests[:2]] == [
        "v7/accounts/1001/meters/2457293/scales/register-day/reading/5316.25/validate",
        "v7/accounts/1001/meters/2457293/scales/register-night/reading/12207.5/validate",
    ]
    method, path, payload = client.requests[2]
    assert (method, path) == ("POST", "v7/accounts/1001/indications")
    UUID(payload.pop("uuid"))
    assert payload == {
        "accountId": 1001,
        "meterId": {"providerId": 1, "registration": "2457293"},
        "serial": "synthetic-legal-meter",
        "scales": [
            {
                "meterScaleId": 1,
                "registerId": "register-day",
                "indicationId": 101,
                "scaleName": "DAY",
                "timeOfUse": "День",
                "registerReading": 5316.25,
                "previousReading": 5315.78,
                "delta": pytest.approx(0.47),
                "indicationDate": None,
            },
            {
                "meterScaleId": 2,
                "registerId": "register-night",
                "indicationId": 102,
                "scaleName": "NIGHT",
                "timeOfUse": "Ночь",
                "registerReading": 12207.5,
                "previousReading": 12206.63,
                "delta": pytest.approx(0.87),
                "indicationDate": None,
            },
        ],
    }
