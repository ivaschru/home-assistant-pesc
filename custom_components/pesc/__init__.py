"""Home Assistant integration for the PESC personal account.

The integration keeps authentication, polling, and meter submissions in one
runtime object per config entry.  Credentials remain inside Home Assistant's
config-entry storage and are never copied into entity attributes or logs.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    PescApiClient,
    PescApiError,
    PescAuthenticationError,
    PescConnectionError,
    PescValidationError,
)
from .const import (
    ATTR_ACCOUNT_ID,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_METER,
    ATTR_READINGS,
    CONF_PROVIDER_CODES,
    CONF_TOTP_SECRET,
    DEFAULT_PROVIDER_CODES,
    DOMAIN,
    SERVICE_SUBMIT_READINGS,
    TOKEN_REFRESH_INTERVAL,
)
from .coordinator import PescDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

SUBMIT_READINGS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_ACCOUNT_ID): vol.Coerce(int),
        vol.Optional(ATTR_METER): cv.string,
        vol.Required(ATTR_READINGS): {cv.string: vol.Coerce(float)},
    }
)


@dataclass(slots=True)
class PescRuntimeData:
    """Runtime objects owned by one loaded PESC config entry."""

    client: PescApiClient
    coordinator: PescDataUpdateCoordinator
    cancel_token_refresh: Any


type PescConfigEntry = ConfigEntry[PescRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration domain and its explicit write action."""

    async def async_submit_readings(call: ServiceCall) -> ServiceResponse:
        """Validate and send readings through the selected loaded entry."""

        loaded_entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and getattr(entry, "runtime_data", None)
        ]
        requested_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if requested_entry_id:
            entries = [
                entry
                for entry in loaded_entries
                if entry.entry_id == requested_entry_id
            ]
        else:
            entries = loaded_entries

        if len(entries) != 1:
            raise ServiceValidationError(
                "Specify config_entry_id when zero or multiple PESC entries are loaded"
            )

        entry: PescConfigEntry = entries[0]
        try:
            result = await entry.runtime_data.client.async_submit_readings(
                call.data[ATTR_ACCOUNT_ID],
                call.data.get(ATTR_METER),
                call.data[ATTR_READINGS],
                snapshot=entry.runtime_data.coordinator.data,
            )
        except PescValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except (PescApiError, PescConnectionError, PescAuthenticationError) as exc:
            raise HomeAssistantError(str(exc)) from exc

        # Refresh immediately so the meter sensors confirm exactly what PESC
        # accepted instead of waiting for the next hourly coordinator cycle.
        await entry.runtime_data.coordinator.async_request_refresh()
        return asdict(result)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SUBMIT_READINGS,
        async_submit_readings,
        schema=SUBMIT_READINGS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PescConfigEntry) -> bool:
    """Authenticate, fetch the first snapshot, and create all platforms."""

    client = PescApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_TOTP_SECRET],
        provider_codes=entry.data.get(CONF_PROVIDER_CODES, DEFAULT_PROVIDER_CODES),
    )
    coordinator = PescDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    async def async_refresh_tokens(_now: Any) -> None:
        """Keep the short-lived PESC tokens warm between hourly polls."""

        try:
            await client.async_refresh_tokens()
        except PescAuthenticationError:
            _LOGGER.warning("PESC token refresh requires reauthentication")
        except PescConnectionError:
            _LOGGER.debug("PESC token refresh could not reach the server")

    cancel_token_refresh = async_track_time_interval(
        hass, async_refresh_tokens, TOKEN_REFRESH_INTERVAL
    )
    entry.runtime_data = PescRuntimeData(
        client=client,
        coordinator=coordinator,
        cancel_token_refresh=cancel_token_refresh,
    )
    entry.async_on_unload(cancel_token_refresh)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PescConfigEntry) -> bool:
    """Unload every entity platform for a config entry."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: PescConfigEntry) -> None:
    """Reload an entry after options or credentials change."""

    await hass.config_entries.async_reload(entry.entry_id)
