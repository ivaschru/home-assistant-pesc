"""Shared entity helpers for the PESC integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PESC_WEB_BASE_URL
from .coordinator import PescDataUpdateCoordinator
from .models import PescAccount, PescMeter


class PescCoordinatorEntity(CoordinatorEntity[PescDataUpdateCoordinator]):
    """Base entity bound to one PESC coordinator and account."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PescDataUpdateCoordinator,
        account_id: int,
    ) -> None:
        super().__init__(coordinator)
        self.account_id = account_id

    @property
    def account(self) -> PescAccount:
        """Return the latest immutable account snapshot."""

        return self.coordinator.data.accounts[self.account_id]

    @property
    def device_info(self) -> DeviceInfo:
        """Represent each personal account/object as a Home Assistant device."""

        return DeviceInfo(
            identifiers={(DOMAIN, f"account_{self.account_id}")},
            name=f"PESC {self.account.alias}",
            manufacturer="PESC",
            model="Personal account",
            configuration_url=(
                f"{PESC_WEB_BASE_URL}/payments/individual/accounts?id={self.account_id}"
            ),
        )


class PescMeterCoordinatorEntity(PescCoordinatorEntity):
    """Base entity associated with a physical meter under an account device."""

    def __init__(
        self,
        coordinator: PescDataUpdateCoordinator,
        account_id: int,
        meter_registration: str,
    ) -> None:
        super().__init__(coordinator, account_id)
        self.meter_registration = meter_registration

    @property
    def meter(self) -> PescMeter:
        """Resolve the meter by its stable provider registration identifier."""

        return next(
            meter
            for meter in self.account.meters
            if meter.registration == self.meter_registration
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Expose each physical meter as a child of its account device."""

        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"meter_{self.account_id}_{self.meter_registration}",
                )
            },
            name=f"PESC {self.account.alias} meter {self.meter.serial}",
            manufacturer="PESC",
            model=self.meter.name,
            serial_number=self.meter.serial,
            via_device=(DOMAIN, f"account_{self.account_id}"),
            configuration_url=(
                f"{PESC_WEB_BASE_URL}/indications/individual/accounts?id={self.account_id}"
            ),
        )
