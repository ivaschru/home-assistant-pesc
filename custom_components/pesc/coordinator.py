"""Data coordinator for the PESC integration."""

from __future__ import annotations

import logging
from time import monotonic

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PescApiClient, PescAuthenticationError, PescConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import PescSnapshot

_LOGGER = logging.getLogger(__name__)
_AUTH_RETRY_DELAYS = (60, 180, 300, 900, 1200)
_REAUTH_AFTER_FAILURES = 3
_RECOVERY_FLOW = "pesc_background_auth_recovery"


class PescDataUpdateCoordinator(DataUpdateCoordinator[PescSnapshot]):
    """Poll every discovered PESC object as one consistent snapshot."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: PescApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self._entry = entry
        self._auth_failures = 0
        self._next_auth_attempt = 0.0

    async def _async_update_data(self) -> PescSnapshot:
        """Fetch current accounts, balances, periods, and meter readings."""

        # A forced update (for example just before sending readings) should
        # respect the same backoff as automatic polls. The cached snapshot
        # remains unavailable until a fresh read actually succeeds.
        retry_in = self._next_auth_attempt - monotonic()
        if retry_in > 0:
            raise UpdateFailed(
                "Waiting to retry PESC authorization", retry_after=retry_in
            )

        try:
            snapshot = await self.client.async_get_snapshot()
        except PescAuthenticationError:
            self._auth_failures += 1
            delay = _AUTH_RETRY_DELAYS[
                min(self._auth_failures, len(_AUTH_RETRY_DELAYS)) - 1
            ]
            self._next_auth_attempt = monotonic() + delay

            # Start the normal credentials form if rejection persists, but
            # keep HA's update scheduler running. At startup HA may recreate
            # the coordinator for each setup retry, so offer the form early.
            if (
                self._auth_failures == _REAUTH_AFTER_FAILURES
                or self._entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            ):
                self._entry.async_start_reauth(
                    self.hass, context={_RECOVERY_FLOW: True}
                )
            raise UpdateFailed(
                "PESC authorization failed; retrying automatically", retry_after=delay
            ) from None
        except PescConnectionError as exc:
            raise UpdateFailed("Unable to connect to PESC") from exc

        self._auth_failures = 0
        self._next_auth_attempt = 0.0
        flow_manager = self.hass.config_entries.flow
        # Clear only the prompt created by this recovery policy and only
        # after a complete, successful account read. Leave other flows alone.
        recovery_flows = tuple(
            flow_manager.async_progress_by_handler(
                DOMAIN,
                match_context={
                    "entry_id": self._entry.entry_id,
                    "source": SOURCE_REAUTH,
                    _RECOVERY_FLOW: True,
                },
            )
        )
        for flow in recovery_flows:
            flow_manager.async_abort(flow["flow_id"])
        return snapshot
