"""Redacted diagnostics for the PESC integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PescConfigEntry
from .const import ACCOUNT_TYPE_INDIVIDUAL, CONF_ACCOUNT_TYPE, CONF_TOTP_SECRET


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PescConfigEntry
) -> dict[str, Any]:
    """Return structural health data without account IDs, addresses, or secrets."""

    snapshot = entry.runtime_data.coordinator.data
    return {
        "entry": {
            "version": entry.version,
            "state": entry.state.value,
            "account_type": entry.data.get(
                CONF_ACCOUNT_TYPE, ACCOUNT_TYPE_INDIVIDUAL
            ),
            "configured_provider_count": len(entry.data.get("provider_codes", [])),
            "has_password": bool(entry.data.get("password")),
            "has_totp_secret": bool(entry.data.get(CONF_TOTP_SECRET)),
        },
        "coordinator": {
            "last_update_success": entry.runtime_data.coordinator.last_update_success,
            "account_count": len(snapshot.accounts),
            "meter_counts": sorted(
                len(account.meters) for account in snapshot.accounts.values()
            ),
            "scale_counts": sorted(
                len(meter.scales)
                for account in snapshot.accounts.values()
                for meter in account.meters
            ),
        },
    }
