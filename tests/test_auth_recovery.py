"""Exercise the coordinator's recovery policy without a Home Assistant install."""

from __future__ import annotations

import importlib
import sys
import types
from enum import Enum, auto
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.pesc.api import PescAuthenticationError
from custom_components.pesc.models import PescSnapshot


class CoordinatorStub[T]:
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass


class UpdateFailedStub(Exception):
    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class EntryState(Enum):
    SETUP_IN_PROGRESS = auto()
    LOADED = auto()


@pytest.fixture
def recovery(monkeypatch):
    """Expose a monotonic clock and record requests to the HA flow manager."""
    module_name = "custom_components.pesc.coordinator"
    stubs = {
        "homeassistant.config_entries": {
            "SOURCE_REAUTH": "reauth",
            "ConfigEntry": object,
            "ConfigEntryState": EntryState,
        },
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": CoordinatorStub,
            "UpdateFailed": UpdateFailedStub,
        },
    }
    for name, attrs in stubs.items():
        module = types.ModuleType(name)
        vars(module).update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    time = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: time[0])
    flow = Mock()
    flow.async_progress_by_handler.return_value = []
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(flow=flow))
    entry = Mock(entry_id="entry-a", state=EntryState.LOADED)
    client = Mock(async_get_snapshot=AsyncMock())
    yield (
        module.PescDataUpdateCoordinator(hass, entry, client),
        client,
        entry,
        flow,
        time,
    )
    sys.modules.pop(module_name, None)


async def test_transient_auth_failure_recovers_without_a_prompt(recovery):
    coordinator, client, entry, _, time = recovery
    fresh = PescSnapshot(accounts={})
    client.async_get_snapshot.side_effect = [
        PescAuthenticationError("Synthetic rejection"),
        fresh,
    ]

    with pytest.raises(UpdateFailedStub) as first:
        await coordinator._async_update_data()
    assert first.value.retry_after == 60

    # A forced read shortly before the timer must not try a second login.
    time[0] += 59
    with pytest.raises(UpdateFailedStub) as early:
        await coordinator._async_update_data()
    assert early.value.retry_after == 1
    assert client.async_get_snapshot.await_count == 1

    time[0] += 1
    assert await coordinator._async_update_data() is fresh
    entry.async_start_reauth.assert_not_called()


async def test_repeated_rejection_keeps_polling_after_requesting_reauth(recovery):
    coordinator, client, entry, flow, time = recovery
    fresh = PescSnapshot(accounts={})
    client.async_get_snapshot.side_effect = [
        *[PescAuthenticationError("Synthetic rejection") for _ in range(6)],
        fresh,
        PescAuthenticationError("Synthetic rejection"),
    ]
    for delay in (60, 180, 300, 900, 1200, 1200):
        with pytest.raises(UpdateFailedStub) as failed:
            await coordinator._async_update_data()
        assert failed.value.retry_after == delay
        time[0] += delay

    context = {"pesc_background_auth_recovery": True}
    entry.async_start_reauth.assert_called_once_with(coordinator.hass, context=context)
    flow.async_progress_by_handler.return_value = iter([{"flow_id": "owned-prompt"}])
    assert await coordinator._async_update_data() is fresh
    flow.async_progress_by_handler.assert_called_once_with(
        "pesc", match_context={"entry_id": "entry-a", "source": "reauth", **context}
    )
    flow.async_abort.assert_called_once_with("owned-prompt")

    with pytest.raises(UpdateFailedStub) as again:
        await coordinator._async_update_data()
    assert again.value.retry_after == 60


async def test_startup_rejection_keeps_setup_retryable(recovery):
    coordinator, client, entry, _, _ = recovery
    entry.state = EntryState.SETUP_IN_PROGRESS
    client.async_get_snapshot.side_effect = PescAuthenticationError(
        "Synthetic rejection"
    )

    # HA turns UpdateFailed during initial setup into ConfigEntryNotReady.
    with pytest.raises(UpdateFailedStub) as failed:
        await coordinator._async_update_data()
    assert failed.value.retry_after == 60
    entry.async_start_reauth.assert_called_once()
