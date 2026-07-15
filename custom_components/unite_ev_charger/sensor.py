"""Sensor entities for the Unite EV Charger."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import control as ctrl
from .const import CHARGER_STATES, CONF_REST_ENABLED, DEFAULT_REST_ENABLED, DOMAIN
from .coordinator import WebastoCoordinator
from .entity import UniteEntity
from .models import WallboxData
from .registers import ChargePointState

STATE_OPTIONS = [s.name.lower() for s in ChargePointState]


@dataclass(frozen=True, kw_only=True)
class UniteSensorDescription(SensorEntityDescription):
    value_fn: Callable[[WallboxData], float | int | str | None]


SENSORS: tuple[UniteSensorDescription, ...] = (
    UniteSensorDescription(
        key="charge_point_state",
        translation_key="charge_point_state",
        device_class=SensorDeviceClass.ENUM,
        options=STATE_OPTIONS,
        value_fn=lambda d: d.state.name.lower() if d.state is not None else None,
    ),
    UniteSensorDescription(
        key="active_power",
        translation_key="active_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.active_power_w,
    ),
    UniteSensorDescription(
        key="current_l1",
        translation_key="current_l1",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.current_l1_a, 2),
    ),
    UniteSensorDescription(
        key="current_l2",
        translation_key="current_l2",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.current_l2_a, 2),
    ),
    UniteSensorDescription(
        key="current_l3",
        translation_key="current_l3",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.current_l3_a, 2),
    ),
    UniteSensorDescription(
        key="voltage_l1",
        translation_key="voltage_l1",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.voltage_l1_v,
    ),
    UniteSensorDescription(
        key="voltage_l2",
        translation_key="voltage_l2",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.voltage_l2_v,
    ),
    UniteSensorDescription(
        key="voltage_l3",
        translation_key="voltage_l3",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.voltage_l3_v,
    ),
    UniteSensorDescription(
        key="session_energy",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: round(d.session_energy_kwh, 3),
    ),
    UniteSensorDescription(
        key="session_duration",
        translation_key="session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda d: d.session_duration_s,
    ),
    UniteSensorDescription(
        key="meter_energy",
        translation_key="meter_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: round(d.meter_energy_kwh, 1),
    ),
    UniteSensorDescription(
        key="set_current",
        translation_key="set_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,  # read-back of register 5004
        value_fn=lambda d: d.set_current_a,
    ),
    # Measured number of phases actually drawing current. This is the real
    # behaviour of the vehicle, which can differ from what the charger reports
    # as its phase setting (register 405) - the cause of the classic "says 3,
    # charges 1" confusion. All control math uses this measured value.
    UniteSensorDescription(
        key="phases_in_use",
        translation_key="phases_in_use",
        icon="mdi:sine-wave",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.phases_in_use,
    ),
    # IEC 61851 status (A/B/C/F) for an external controller such as evcc.
    UniteSensorDescription(
        key="evcc_status",
        translation_key="evcc_status",
        icon="mdi:ev-station",
        value_fn=lambda d: d.iec61851_status,
    ),
    # Raw phase capability register 404 (0 = 1-phase, 1 = 3-phase), re-read every
    # cycle because firmware can report it incorrectly while booting.
    UniteSensorDescription(
        key="register_404",
        translation_key="register_404",
        icon="mdi:numeric",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.phase_capability_raw,
    ),
    # Raw phase register 405 (0 = 1-phase, 1 = 3-phase), for diagnostics.
    UniteSensorDescription(
        key="register_405",
        translation_key="register_405",
        icon="mdi:numeric",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.phase_switch_raw,
    ),
)


@dataclass(frozen=True, kw_only=True)
class UniteControllerSensorDescription(SensorEntityDescription):
    """A sensor whose value comes from the control engine, not the wallbox."""

    value_fn: Callable[[object], float | int | None]


CONTROLLER_SENSORS: tuple[UniteControllerSensorDescription, ...] = (
    UniteControllerSensorDescription(
        key="available_surplus",
        translation_key="available_surplus",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: round(c.available_surplus_w) if c.available_surplus_w is not None else None,
    ),
    UniteControllerSensorDescription(
        key="control_setpoint",
        translation_key="control_setpoint",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.computed_setpoint,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [UniteSensor(coordinator, d) for d in SENSORS]
    entities += [UniteControllerSensor(coordinator, d) for d in CONTROLLER_SENSORS]
    entities += [
        UniteChargerStateSensor(coordinator),
        UniteRecoveryStatusSensor(coordinator),
        UniteRecoveryRemainingSensor(coordinator),
        UniteLastRecoverySensor(coordinator),
    ]
    if entry.options.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
        entities.append(UniteLastRestartSensor(coordinator))  # only with REST on
    async_add_entities(entities)


class UniteSensor(UniteEntity, SensorEntity):
    entity_description: UniteSensorDescription

    def __init__(
        self, coordinator: WebastoCoordinator, description: UniteSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class UniteControllerSensor(UniteEntity, SensorEntity):
    entity_description: UniteControllerSensorDescription

    def __init__(
        self, coordinator: WebastoCoordinator, description: UniteControllerSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        controller = self.coordinator.controller
        if controller is None:
            return None
        return self.entity_description.value_fn(controller)


class UniteChargerStateSensor(UniteEntity, SensorEntity):
    """Interpreted "what is the charger doing" state (the State Inspector).

    Composed from state we already track - connection, recovery, phase, fault -
    so it reads as one meaningful status instead of raw registers.
    """

    _attr_translation_key = "charger_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CHARGER_STATES)

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "charger_state")

    @property
    def available(self) -> bool:
        return True  # must stay available to report 'disconnected'

    @property
    def native_value(self) -> str | None:
        coord = self.coordinator
        until = coord.rest_restart_until
        if until is not None and monotonic() < until:
            return "restarting"
        if not coord.last_update_success:
            return "disconnected"
        data = coord.data
        if data is None:
            return None
        controller = coord.controller
        mismatch = ctrl.is_phase_mismatch(
            data.charging, data.phase_switch_raw,
            data.current_l1_a, data.current_l2_a, data.current_l3_a,
        )
        return ctrl.derive_charger_state(
            connection_ok=True,
            restarting=False,
            faulted=data.faulted,
            vehicle_connected=data.vehicle_connected,
            charging=data.charging,
            phase_mismatch=mismatch,
            recovery_active=controller.recovery_active if controller is not None else False,
        )


class UniteLastRecoverySensor(UniteEntity, SensorEntity):
    """Diagnostic: when the last phase-recovery ran, with its outcome."""

    _attr_translation_key = "last_recovery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "last_recovery")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        controller = self.coordinator.controller
        return None if controller is None else controller.last_recovery_at

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        controller = self.coordinator.controller
        return {} if controller is None else {"result": controller.last_recovery_result}


class UniteLastRestartSensor(UniteEntity, SensorEntity):
    """Diagnostic: when the web-UI restart was last requested, and its result.

    On-demand only - it is written by the restart button, never by polling.
    """

    _attr_translation_key = "last_restart"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "last_restart")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.rest_last_restart_at

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"result": self.coordinator.rest_last_restart_result}


class UniteRecoveryStatusSensor(UniteEntity, SensorEntity):
    """Diagnostic: state of the optional 1->3 phase recovery sequence."""

    _attr_translation_key = "recovery_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "recovery_status")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        controller = self.coordinator.controller
        return None if controller is None else controller.recovery_status

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        controller = self.coordinator.controller
        if controller is None:
            return {}
        return {
            "active": controller.recovery_active,
            "recovery_enabled": controller.cfg.phase_recovery_enabled,
            "requested_phase": controller.requested_phase,
        }


class UniteRecoveryRemainingSensor(UniteEntity, SensorEntity):
    """Diagnostic: seconds left in the current recovery phase.

    Deliberately has NO state_class: it is a live countdown that is 0 almost all
    the time, so long-term statistics would only bloat the recorder (and adding a
    state_class later and removing it triggers HA's "orphaned statistics" nag).
    """

    _attr_translation_key = "recovery_remaining"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "recovery_remaining")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> int | None:
        controller = self.coordinator.controller
        return None if controller is None else controller.recovery_remaining_s
