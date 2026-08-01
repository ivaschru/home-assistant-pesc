"""Shared entity helpers for the PESC integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACCOUNT_TYPE_LEGAL,
    DOMAIN,
    PESC_INDIVIDUAL_WEB_BASE_URL,
    PESC_LEGAL_WEB_BASE_URL,
)
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
        account = coordinator.data.accounts[account_id]
        self.pesc_unique_account_id = (
            f"legal_{account_id}"
            if account.account_type == ACCOUNT_TYPE_LEGAL
            else str(account_id)
        )

    @property
    def account(self) -> PescAccount:
        """Return the latest immutable account snapshot."""

        return self.coordinator.data.accounts[self.account_id]

    @property
    def device_info(self) -> DeviceInfo:
        """Represent each personal account/object as a Home Assistant device."""

        is_legal = self.account.account_type == ACCOUNT_TYPE_LEGAL
        identifier = (
            f"legal_account_{self.account_id}"
            if is_legal
            else f"account_{self.account_id}"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=f"PESC {self.account.alias}",
            manufacturer="PESC",
            model="Legal account" if is_legal else "Individual account",
            configuration_url=(
                f"{PESC_LEGAL_WEB_BASE_URL}/payments/legal"
                f"?account={self.account_id}"
                if is_legal
                else f"{PESC_INDIVIDUAL_WEB_BASE_URL}/payments/individual/"
                f"accounts?id={self.account_id}"
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

        is_legal = self.account.account_type == ACCOUNT_TYPE_LEGAL
        account_identifier = (
            f"legal_account_{self.account_id}"
            if is_legal
            else f"account_{self.account_id}"
        )
        meter_identifier = (
            f"legal_meter_{self.account_id}_{self.meter_registration}"
            if is_legal
            else f"meter_{self.account_id}_{self.meter_registration}"
        )
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    meter_identifier,
                )
            },
            name=f"PESC {self.account.alias} meter {self.meter.serial}",
            manufacturer="PESC",
            model=self.meter.name,
            serial_number=self.meter.serial,
            via_device=(DOMAIN, account_identifier),
            configuration_url=(
                f"{PESC_LEGAL_WEB_BASE_URL}/indications/legal"
                f"?account={self.account_id}"
                if is_legal
                else f"{PESC_INDIVIDUAL_WEB_BASE_URL}/indications/individual/"
                f"accounts?id={self.account_id}"
            ),
        )
