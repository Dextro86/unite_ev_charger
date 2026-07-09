"""Manual charge-current setpoint."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ABS_MAX_CURRENT_A, DOMAIN, MODE_MANUAL
from .coordinator import WebastoCoordinator
from .entity import UniteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UniteChargeCurrentNumber(coordinator)])


class UniteChargeCurrentNumber(UniteEntity, NumberEntity, RestoreEntity):
    """Charge current used in Manual mode."""

    _attr_translation_key = "charge_current"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "charge_current")

    @property
    def available(self) -> bool:
        # Only effective as the live setpoint in external (evcc) mode or in the
        # internal Manual mode; greyed out in Fast/Solar/Min+Solar.
        ctl = self.coordinator.controller
        return super().available and (ctl.is_external or ctl.mode == MODE_MANUAL)

    @property
    def native_min_value(self) -> float:
        # External (evcc setMaxCurrent) allows 0 (= stop); internal uses the min.
        return 0 if self.coordinator.controller.is_external else self.coordinator.controller.cfg.min_current

    @property
    def native_max_value(self) -> float:
        if self.coordinator.controller.is_external:
            return ABS_MAX_CURRENT_A
        return self.coordinator.controller.limits.effective_max()

    @property
    def native_value(self) -> float | None:
        ctl = self.coordinator.controller
        if ctl.is_external:
            # Report evcc's own last setMaxCurrent intent (not the transient
            # hardware value, which a recovery pause forces to 0 A).
            intent = ctl.ext_current_intent
            if intent is not None:
                return intent
            data = self.coordinator.data
            return None if data is None else data.set_current_a
        return ctl.manual_current

    async def async_set_native_value(self, value: float) -> None:
        ctl = self.coordinator.controller
        if ctl.is_external:
            await ctl.async_external_set_current(value)
            return
        ctl.manual_current = max(ctl.cfg.min_current, min(int(value), ctl.limits.effective_max()))
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if (
            last is not None
            and last.state not in (None, "unknown", "unavailable")
            and not self.coordinator.controller.is_external
        ):
            try:
                self.coordinator.controller.manual_current = int(float(last.state))
            except (TypeError, ValueError):
                pass
