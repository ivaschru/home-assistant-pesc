"""Exercise real validation boundaries without sending any provider writes."""

from dataclasses import replace

import pytest
from test_api import LegalRecordingClient, _snapshot

from custom_components.pesc.api import (
    PescApiError,
    PescConnectionError,
    PescHighConsumptionWarning,
    PescValidationError,
)
from custom_components.pesc.submission import async_submit_readings_response


def legal_snapshot():
    """Use synthetic identities and complete register metadata."""
    base = _snapshot(digits_right=2)
    account = base.accounts[123]
    meter = account.meters[0]
    scales = tuple(
        replace(scale, register_id=f"register-{scale.name}", indication_id=i)
        for i, scale in enumerate(meter.scales, 1)
    )
    account = replace(account, meters=(replace(meter, scales=scales),))
    return replace(base, accounts={123: account})


PERIOD = "July|2026-07-01|2026-07-31"
READINGS = {"Day": 125, "Night": 150}


class WarningClient(LegalRecordingClient):
    """Inject a register warning or a final-write error and record every call."""

    def __init__(self, *, code="LKK_SERVICE_054", status=416, final=False):
        super().__init__()
        self.code, self.status, self.final = code, status, final
        self.fresh = legal_snapshot()
        self.refreshes = 0

    async def async_get_snapshot(self):
        self.refreshes += 1
        return self.fresh

    async def _request(self, method, path, *, payload=None):
        self.requests.append((method, path, payload))
        if (self.final and path.endswith("/indications")) or (
            not self.final and "register-Day/" in path
        ):
            raise PescApiError(
                "Ваш расход больше обычного", code=self.code, http_status=self.status
            )
        return {}


async def test_warning_preserves_exact_provider_reason_and_requires_confirmation():
    client = WarningClient()
    result = await async_submit_readings_response(
        client,
        123,
        None,
        READINGS,
        snapshot=legal_snapshot(),
        return_errors=True,
    )
    assert result == {
        "accepted": False,
        "error": "Ваш расход больше обычного",
        "error_code": "LKK_SERVICE_054",
        "http_status": 416,
        "error_kind": "provider_rejection",
        "confirmation_required": True,
    }
    assert not any(path.endswith("/indications") for _, path, _ in client.requests)
    with pytest.raises(PescHighConsumptionWarning):
        await async_submit_readings_response(
            client, 123, None, READINGS, snapshot=legal_snapshot()
        )


async def test_confirmation_refreshes_and_validates_other_scale_before_one_write():
    client = WarningClient()
    result = await client.async_submit_readings(
        123,
        None,
        READINGS,
        snapshot=legal_snapshot(),
        confirm_high_consumption=True,
        expected_period=PERIOD,
    )
    assert result.accepted
    assert client.refreshes == 1
    assert len(client.requests) == 3
    assert "register-Night/" in client.requests[1][1]
    assert client.requests[2][1].endswith("/indications")
    assert [scale["registerReading"] for scale in client.requests[2][2]["scales"]] == [
        125,
        150,
    ]


@pytest.mark.parametrize(
    "code,status",
    [("another_warning", 416), ("LKK_SERVICE_054", 400), ("LKK_SERVICE_054", 500)],
)
async def test_confirmation_never_ignores_other_rejections(code, status):
    client = WarningClient(code=code, status=status)
    with pytest.raises(PescApiError):
        await client.async_submit_readings(
            123, None, READINGS, confirm_high_consumption=True, expected_period=PERIOD
        )
    assert not any(path.endswith("/indications") for _, path, _ in client.requests)


@pytest.mark.parametrize(
    "state,period",
    [("REGISTERED", PERIOD), ("AVAILABLE", "another period"), ("AVAILABLE", None)],
)
async def test_confirmation_fences_fresh_registered_state_and_period(state, period):
    client = WarningClient()
    client.fresh.accounts[123] = replace(
        client.fresh.accounts[123], reading_state=state
    )
    with pytest.raises(PescValidationError):
        await client.async_submit_readings(
            123,
            None,
            READINGS,
            snapshot=legal_snapshot(),
            confirm_high_consumption=True,
            expected_period=period,
        )
    assert client.refreshes == 1
    assert client.requests == []


async def test_final_warning_cannot_arm_a_confirmation_and_is_not_retried():
    client = WarningClient(final=True)
    result = await async_submit_readings_response(
        client,
        123,
        None,
        READINGS,
        snapshot=legal_snapshot(),
        return_errors=True,
    )
    assert not result["confirmation_required"]
    assert len([p for _, p, _ in client.requests if p.endswith("/indications")]) == 1


async def test_transport_failure_is_not_reported_as_a_provider_rejection():
    class BrokenClient:
        async def async_submit_readings(self, *args, **kwargs):
            raise PescConnectionError("Request timed out")

    result = await async_submit_readings_response(
        BrokenClient(), 123, None, READINGS, snapshot=None, return_errors=True
    )
    assert result["error_kind"] == "connection"
    assert result["confirmation_required"] is False


async def test_local_monotonicity_still_applies_during_confirmation():
    client = WarningClient()
    with pytest.raises(PescValidationError, match="lower"):
        await client.async_submit_readings(
            123,
            None,
            {"Day": 99, "Night": 150},
            confirm_high_consumption=True,
            expected_period=PERIOD,
        )
    assert client.requests == []
