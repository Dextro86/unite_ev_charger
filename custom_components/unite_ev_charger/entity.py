"""Shared entity base for the Unite EV Charger integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo as HaDeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WebastoCoordinator


class UniteEntity(CoordinatorEntity[WebastoCoordinator]):
    """Base entity tying everything to the wallbox device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WebastoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.device_unique_id}_{key}"

    @property
    def device_info(self) -> HaDeviceInfo:
        dev = self.coordinator.device
        return HaDeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_unique_id)},
            name="Unite EV Charger",
            manufacturer="Webasto / Vestel",
            model="Unite / EVC-04",
            sw_version=dev.firmware_version,
            serial_number=dev.serial_number,
            # "Visit device" link on the device page -> opens the charger web UI.
            configuration_url=f"http://{self.coordinator.client.host}",
        )
