"""Binary sensors for PESC meter-reading availability."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PescRuntimeData
from .coordinator import PescDataUpdateCoordinator
from .entity import PescCoordinatorEntity


class PescReadingsAcceptedBinarySensor(PescCoordinatorEntity, BinarySensorEntity):
    """Whether PESC currently allows readings for an account."""

    _attr_translation_key = "readings_accepted"

    def __init__(
        self,
        coordinator: PescDataUpdateCoordinator,
        account_id: int,
    ) -> None:
        super().__init__(coordinator, account_id)
        self._attr_unique_id = f"{account_id}_readings_accepted"

    @property
    def is_on(self) -> bool:
        period = self.account.reading_period
        return self.account.reading_state == "AVAILABLE" and not (
            period and period.forbidden
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        period = self.account.reading_period
        return {
            "reading_state": self.account.reading_state,
            "period_name": period.name if period else None,
            "date_from": period.date_from if period else None,
            "date_to": period.date_to if period else None,
            "deadline_day": period.deadline_day if period else None,
            "message": period.message if period else None,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[PescRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one availability entity per discovered account."""

    coordinator = entry.runtime_data.coordinator
    known: set[int] = set()

    @callback
    def async_discover_entities() -> None:
        new_account_ids = set(coordinator.data.accounts) - known
        if not new_account_ids:
            return
        known.update(new_account_ids)
        async_add_entities(
            [
                PescReadingsAcceptedBinarySensor(coordinator, account_id)
                for account_id in sorted(new_account_ids)
            ]
        )

    async_discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_discover_entities))
