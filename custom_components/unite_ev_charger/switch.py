"""Charging on/off switch."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import WebastoCoordinator
from .entity import UniteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            UniteAutomaticControlSwitch(coordinator),
            UniteChargingSwitch(coordinator),
        ]
    )


class UniteAutomaticControlSwitch(UniteEntity, SwitchEntity):
    """Persistent EMS ownership switch."""

    _attr_translation_key = "automatic_control"

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "automatic_control")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.automatic_control_requested

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_activate()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        restored = await self.coordinator.async_suspend(preserve_requested=False)
        if not restored:
            raise HomeAssistantError(
                "Automatic control is off, but charger restoration failed; "
                "see integration logs"
            )


class UniteChargingSwitch(UniteEntity, SwitchEntity, RestoreEntity):
    """Master 'allow charging' toggle (default on)."""

    _attr_translation_key = "charging"

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "charging_enabled")

    @property
    def is_on(self) -> bool | None:
        controller = self.coordinator.controller
        if controller.is_external:
            # evcc 'enabled': report evcc's own last intent so it stays in sync
            # even while a recovery pause briefly forces the hardware to 0 A.
            intent = controller.ext_enabled_intent
            if intent is not None:
                return intent
            data = self.coordinator.data
            return None if data is None else (data.set_current_a or 0) > 0
        return controller.charging_enabled

    async def async_turn_on(self, **kwargs) -> None:
        controller = self.coordinator.controller
        if controller.is_external:
            await controller.async_external_set_enabled(True)
            return
        controller.charging_enabled = True
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        controller = self.coordinator.controller
        if controller.is_external:
            await controller.async_external_set_enabled(False)
            return
        controller.charging_enabled = False
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Only restore a genuine on/off; keep the default (on) for
        # unknown/unavailable (e.g. after a remove + re-add).
        if (
            last is not None
            and last.state in ("on", "off")
            and not self.coordinator.controller.is_external
        ):
            self.coordinator.controller.charging_enabled = last.state == "on"
