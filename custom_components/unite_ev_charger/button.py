"""Restart button - reboots the wallbox via its web API (opt-in REST).

Only created when REST is enabled in the options. Completely separate from the
Modbus control path; after the reboot the wallbox drops the Modbus connection
and the coordinator's reconnect handshake re-claims it (failsafe + current +
phase + alive).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import monotonic

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DOMAIN,
    REST_RESTART_COOLDOWN_S,
)
from .coordinator import WebastoCoordinator
from .entity import UniteEntity
from .rest_client import UniteRestAuthError, UniteRestError, async_restart_charger

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not entry.options.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
        return  # REST opt-in is off -> no restart button
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UniteRestartButton(coordinator, entry)])


class UniteRestartButton(UniteEntity, ButtonEntity):
    """Reboot the wallbox through its web UI."""

    _attr_translation_key = "restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WebastoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, "restart")
        self._entry = entry

    async def async_press(self) -> None:
        now = monotonic()
        until = self.coordinator.rest_restart_until
        if until is not None and now < until:
            raise HomeAssistantError(
                "The charger was just asked to restart; wait a moment before retrying."
            )
        host = self._entry.data[CONF_HOST]
        username = self._entry.options.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME)
        password = self._entry.options.get(CONF_REST_PASSWORD, "")
        session = async_get_clientsession(self.hass)
        try:
            await async_restart_charger(session, host, username, password)
        except UniteRestAuthError as err:
            self._record_result("auth_failed")
            raise HomeAssistantError(f"Web UI login failed: {err}") from err
        except UniteRestError as err:
            self._record_result("unreachable")
            raise HomeAssistantError(f"Could not restart the charger: {err}") from err
        self._record_result("success")
        # Marks the recovery window: blocks repeat presses and drives the
        # 'restarting' charger state until the wallbox is back (~5 min).
        self.coordinator.rest_restart_until = now + REST_RESTART_COOLDOWN_S
        _LOGGER.info("Wallbox restart requested via the web API")

    def _record_result(self, result: str) -> None:
        self.coordinator.rest_last_restart_at = datetime.now(timezone.utc)
        self.coordinator.rest_last_restart_result = result
        self.coordinator.async_update_listeners()  # refresh the diagnostic sensor now
