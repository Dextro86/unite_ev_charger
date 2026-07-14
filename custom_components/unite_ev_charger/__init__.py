"""The Unite EV Charger integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
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

    try:
        await coordinator.async_claim_connection()
        await coordinator.async_read_device_info()
    except WebastoModbusError as err:
        await client.async_close()
        raise ConfigEntryNotReady(f"Could not reach the charger: {err}") from err

    coordinator.controller = ChargeControl(hass, entry, coordinator)
    # Initial claim happens before the controller exists. Re-apply controller
    # intent now; external mode starts at 0 A until its owner sends a command.
    await coordinator.controller.async_on_reconnect(None)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: WebastoCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator.controller is not None:
            await coordinator.controller.async_shutdown()
        await coordinator.client.async_close()
    return unloaded
