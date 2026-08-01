"""Sensor entities for PESC balances, bills, deadlines, and meter scales."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PescRuntimeData
from .coordinator import PescDataUpdateCoordinator
from .entity import PescCoordinatorEntity, PescMeterCoordinatorEntity
from .models import PescAccount, PescMeterScale


@dataclass(frozen=True, kw_only=True)
class PescAccountSensorDescription(SensorEntityDescription):
    """Description for a value read directly from an account snapshot."""

    value_fn: Callable[[PescAccount], Any]


ACCOUNT_SENSORS: tuple[PescAccountSensorDescription, ...] = (
    PescAccountSensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="RUB",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda account: account.balance,
    ),
    PescAccountSensorDescription(
        key="amount_due",
        translation_key="amount_due",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="RUB",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda account: account.amount_due,
    ),
    PescAccountSensorDescription(
        key="overpayment",
        translation_key="overpayment",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="RUB",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda account: account.overpayment,
    ),
    PescAccountSensorDescription(
        key="current_bill",
        translation_key="current_bill",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="RUB",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda account: account.current_bill_amount,
    ),
    PescAccountSensorDescription(
        key="reading_deadline",
        translation_key="reading_deadline",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda account: (
            account.reading_period.date_to if account.reading_period else None
        ),
    ),
)


class PescAccountSensor(PescCoordinatorEntity, SensorEntity):
    """One account-level PESC sensor."""

    entity_description: PescAccountSensorDescription

    def __init__(
        self,
        coordinator: PescDataUpdateCoordinator,
        account_id: int,
        description: PescAccountSensorDescription,
    ) -> None:
        super().__init__(coordinator, account_id)
        self.entity_description = description
        self._attr_unique_id = f"{self.pesc_unique_account_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the latest value selected by the entity description."""

        return self.entity_description.value_fn(self.account)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful non-secret account metadata and period context."""

        attributes: dict[str, Any] = {
            "account_id": self.account.account_id,
            "account_number": self.account.account_number,
            "provider_code": self.account.provider_code,
            "auto_payment_on": self.account.auto_payment_on,
            "delivery": self.account.delivery,
        }
        if self.entity_description.key == "current_bill":
            attributes["bill_timestamp"] = self.account.current_bill_timestamp
        if (
            self.entity_description.key == "reading_deadline"
            and self.account.reading_period
        ):
            period = self.account.reading_period
            attributes.update(
                {
                    "period_name": period.name,
                    "date_from": period.date_from,
                    "deadline_day": period.deadline_day,
                    "message": period.message,
                }
            )
        return attributes


class PescMeterScaleSensor(PescMeterCoordinatorEntity, SensorEntity):
    """Last reading accepted by PESC for one meter tariff scale."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: PescDataUpdateCoordinator,
        account_id: int,
        meter_registration: str,
        scale_id: int,
    ) -> None:
        super().__init__(coordinator, account_id, meter_registration)
        self.scale_id = scale_id
        self._attr_unique_id = (
            f"{self.pesc_unique_account_id}_{meter_registration}_"
            f"scale_{scale_id}_last_reading"
        )

    @property
    def scale(self) -> PescMeterScale:
        """Return the latest matching scale from the coordinator snapshot."""

        return next(
            scale for scale in self.meter.scales if scale.scale_id == self.scale_id
        )

    @property
    def name(self) -> str:
        """Use the provider's scale name, for example Day or Night."""

        return self.scale.name

    @property
    def native_value(self) -> float | None:
        """Return the last reading accepted by PESC."""

        return self.scale.previous_reading

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "scale_id": self.scale.scale_id,
            "previous_reading_date": self.scale.previous_reading_date,
            "meter_status": self.meter.status,
            "meter_registration": self.meter.registration,
        }


def _entity_key(entity: SensorEntity) -> str:
    """Return a stable key before Home Assistant assigns an entity_id."""

    return str(entity.unique_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[PescRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create current sensors and add newly discovered meters dynamically."""

    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def async_discover_entities() -> None:
        entities: list[SensorEntity] = []
        for account in coordinator.data.accounts.values():
            entities.extend(
                PescAccountSensor(coordinator, account.account_id, description)
                for description in ACCOUNT_SENSORS
            )
            for meter in account.meters:
                entities.extend(
                    PescMeterScaleSensor(
                        coordinator,
                        account.account_id,
                        meter.registration,
                        scale.scale_id,
                    )
                    for scale in meter.scales
                )

        new_entities = [
            entity for entity in entities if _entity_key(entity) not in known
        ]
        if not new_entities:
            return
        known.update(_entity_key(entity) for entity in new_entities)
        async_add_entities(new_entities)

    async_discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_discover_entities))
