"""Shared entity base for the Unite EV Charger integration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT as BINARY_SENSOR_FORMAT,
    BinarySensorEntity,
)
from homeassistant.components.button import (
    ENTITY_ID_FORMAT as BUTTON_FORMAT,
    ButtonEntity,
)
from homeassistant.components.number import (
    ENTITY_ID_FORMAT as NUMBER_FORMAT,
    NumberEntity,
)
from homeassistant.components.select import (
    ENTITY_ID_FORMAT as SELECT_FORMAT,
    SelectEntity,
)
from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT as SENSOR_FORMAT,
    SensorEntity,
)
from homeassistant.components.switch import (
    ENTITY_ID_FORMAT as SWITCH_FORMAT,
    SwitchEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo as HaDeviceInfo
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WebastoCoordinator

# Each platform entity type paired with its "<domain>.{}" entity_id template.
_ENTITY_ID_FORMATS: tuple[tuple[type, str], ...] = (
    (BinarySensorEntity, BINARY_SENSOR_FORMAT),
    (ButtonEntity, BUTTON_FORMAT),
    (NumberEntity, NUMBER_FORMAT),
    (SelectEntity, SELECT_FORMAT),
    (SwitchEntity, SWITCH_FORMAT),
    (SensorEntity, SENSOR_FORMAT),
)


class UniteEntity(CoordinatorEntity[WebastoCoordinator]):
    """Base entity tying everything to the wallbox device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WebastoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.device_unique_id}_{key}"
        # Force a stable, language-independent entity_id from the English key
        # (e.g. sensor.unite_ev_charger_charger_state) instead of letting HA
        # derive it from the localized display name, so the entity_ids are the
        # same regardless of the Home Assistant language. The platform is taken
        # from the concrete entity type this base is mixed into.
        for entity_type, id_format in _ENTITY_ID_FORMATS:
            if isinstance(self, entity_type):
                self.entity_id = async_generate_entity_id(
                    id_format, f"unite_ev_charger_{key}", hass=coordinator.hass
                )
                break

    @property
    def available(self) -> bool:
        """Entities backed by charger state require active EMS ownership."""
        return super().available and self.coordinator.ownership_active

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
