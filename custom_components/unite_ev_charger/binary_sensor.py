"""Binary sensor entities for the Unite EV Charger."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import control as ctrl
from .const import DOMAIN, PHASE_3P
from .coordinator import WebastoCoordinator
from .entity import UniteEntity


def _phase_mismatch(coordinator: WebastoCoordinator) -> bool:
    """Only a real mismatch when 3-phase was actively requested (not the resting
    405 default), so a 1-phase car never trips it. See control.is_phase_mismatch."""
    d = coordinator.data
    controller = coordinator.controller
    requested_3p = controller is not None and controller.requested_phase == PHASE_3P
    return ctrl.is_phase_mismatch(
        d.charging, requested_3p, d.current_l1_a, d.current_l2_a, d.current_l3_a
    )


@dataclass(frozen=True, kw_only=True)
class UniteBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[WebastoCoordinator], bool]


BINARY_SENSORS: tuple[UniteBinaryDescription, ...] = (
    UniteBinaryDescription(
        key="vehicle_connected",
        translation_key="vehicle_connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda c: c.data.vehicle_connected,
    ),
    UniteBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda c: c.data.charging,
    ),
    UniteBinaryDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c: c.data.faulted,
    ),
    # 3-phase actively requested but the car draws single-phase -> the classic
    # "asked 3, charges 1" stuck upshift, made directly visible.
    UniteBinaryDescription(
        key="phase_mismatch",
        translation_key="phase_mismatch",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_phase_mismatch,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        UniteBinarySensor(coordinator, desc) for desc in BINARY_SENSORS
    ]
    entities.append(UniteConnectionSensor(coordinator))
    async_add_entities(entities)


class UniteBinarySensor(UniteEntity, BinarySensorEntity):
    entity_description: UniteBinaryDescription

    def __init__(
        self, coordinator: WebastoCoordinator, description: UniteBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator)


class UniteConnectionSensor(UniteEntity, BinarySensorEntity):
    """Modbus connection health. Stays available so it can report 'off'."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        return True  # unlike the others, report state even when disconnected

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        stats = self.coordinator.client.stats
        return {
            "reconnects": stats.reconnects,
            "read_failures": stats.read_failures,
            "write_failures": stats.write_failures,
            "timeouts": stats.timeouts,
            "alive_failures": stats.alive_failures,
            "last_response_ms": stats.last_response_ms,
            "avg_response_ms": round(stats.avg_response_ms) if stats.avg_response_ms is not None else None,
            "last_error": stats.last_error,
        }
