"""Charge mode selector."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CHARGE_MODES, DOMAIN, PHASE_PREFERENCES
from .coordinator import WebastoCoordinator
from .entity import UniteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    controller = coordinator.controller

    entities: list[SelectEntity] = [UniteModeSelect(coordinator)]
    # Only the phase select that matches the current mode/toggle is created, so
    # you never see an irrelevant (undeletable) one. Changing mode reloads the
    # entry, after which the other one is created (and the old becomes a
    # deletable orphan).
    if controller.is_external:
        entities.append(UnitePhaseSelect(coordinator))        # evcc phaseswitch
    elif controller.cfg.phase_switching:
        entities.append(UnitePhasePreferenceSelect(coordinator))  # Auto/1/3

    async_add_entities(entities)


class UniteModeSelect(UniteEntity, SelectEntity, RestoreEntity):
    """The live charge mode (fast / manual / solar / min_solar).

    Only meaningful with the built-in control loop; hidden under external (evcc)
    control, which owns the charging strategy.
    """

    _attr_translation_key = "mode"
    _attr_options = list(CHARGE_MODES)

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.controller.is_external

    @property
    def current_option(self) -> str:
        return self.coordinator.controller.mode

    async def async_select_option(self, option: str) -> None:
        if option in CHARGE_MODES:
            self.coordinator.controller.mode = option
            await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in CHARGE_MODES:
            self.coordinator.controller.mode = last.state


class UnitePhaseSelect(UniteEntity, SelectEntity):
    """Phase selector ("1"/"3") for an external controller (evcc phaseswitch).

    Writes register 405 directly. Only exposed under external control; with the
    built-in loop, phase switching is automatic.
    """

    _attr_translation_key = "phase_select"
    _attr_options = ["1", "3"]

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "phase_select")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.controller.is_external

    @property
    def current_option(self) -> str | None:
        # Report evcc's own last phase request so it does not keep re-issuing the
        # switch while a recovery pause is deliberately waiting.
        requested = self.coordinator.controller.requested_phase
        if requested in ("1", "3"):
            return requested
        data = self.coordinator.data
        if data is None or data.phase_switch_raw is None:
            return None
        return "3" if data.phase_switch_raw == 1 else "1"

    async def async_select_option(self, option: str) -> None:
        if option in ("1", "3"):
            await self.coordinator.controller.async_external_set_phase(int(option))


class UnitePhasePreferenceSelect(UniteEntity, SelectEntity, RestoreEntity):
    """Phase preference for the built-in control loop: Auto / 1 / 3.

    Auto = solar follows surplus, fast/manual use all phases. Unlike the mode,
    this persists (it is not reset when the car is unplugged). Hidden under
    external (evcc) control.
    """

    _attr_translation_key = "phase_preference"
    _attr_options = list(PHASE_PREFERENCES)

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "phase_preference")

    @property
    def available(self) -> bool:
        controller = self.coordinator.controller
        return super().available and not controller.is_external and controller.cfg.phase_switching

    @property
    def current_option(self) -> str:
        return self.coordinator.controller.phase_preference

    async def async_select_option(self, option: str) -> None:
        if option in PHASE_PREFERENCES:
            self.coordinator.controller.phase_preference = option
            await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in PHASE_PREFERENCES:
            self.coordinator.controller.phase_preference = last.state
