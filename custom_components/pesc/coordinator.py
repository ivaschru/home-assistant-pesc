"""Data coordinator for the PESC integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PescApiClient, PescAuthenticationError, PescConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import PescSnapshot

_LOGGER = logging.getLogger(__name__)


class PescDataUpdateCoordinator(DataUpdateCoordinator[PescSnapshot]):
    """Poll every discovered PESC object as one consistent snapshot."""

    def __init__(self, hass: HomeAssistant, client: PescApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> PescSnapshot:
        """Fetch current accounts, balances, periods, and meter readings."""

        try:
            return await self.client.async_get_snapshot()
        except PescAuthenticationError as exc:
            # Raising ConfigEntryAuthFailed opens Home Assistant's re-auth flow
            # instead of retrying bad credentials forever in the background.
            raise ConfigEntryAuthFailed from exc
        except PescConnectionError as exc:
            raise UpdateFailed("Unable to connect to PESC") from exc
