"""The Unite EV Charger integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_UNIT_ID,
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    DOMAIN,
    PLATFORMS,
)
from .controller import ChargeControl
from .coordinator import WebastoCoordinator
from .modbus import WebastoModbus, WebastoModbusError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unite EV Charger from a config entry."""
    client = WebastoModbus(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        unit_id=entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
    )
    coordinator = WebastoCoordinator(hass, entry, client)
    coordinator.controller = ChargeControl(hass, entry, coordinator)

    try:
        if coordinator.automatic_control_requested:
            await coordinator.async_activate()
            await coordinator.async_read_device_info()
            await coordinator.async_config_entry_first_refresh()
        elif coordinator.ownership_dirty:
            restored = await coordinator.async_suspend(preserve_requested=True)
            if not restored:
                raise WebastoModbusError(
                    "Could not finish pending EMS ownership restoration"
                )
    except WebastoModbusError as err:
        raise ConfigEntryNotReady(f"Could not reach the charger: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    async def _async_shutdown(_event) -> None:
        restored = await coordinator.async_suspend(preserve_requested=True)
        if not restored:
            _LOGGER.critical(
                "Home Assistant is stopping before charger configuration could "
                "be restored; recovery snapshot retained"
            )

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)
    )
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and not coordinator.configuration_changed(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    restored = await coordinator.async_suspend(preserve_requested=True)
    if not restored:
        _LOGGER.critical(
            "Refusing to unload before charger configuration is restored"
        )
        return False

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    elif coordinator.automatic_control_requested:
        try:
            await coordinator.async_activate()
        except WebastoModbusError as err:
            _LOGGER.error("Could not resume control after unload failed: %s", err)
    return unloaded
