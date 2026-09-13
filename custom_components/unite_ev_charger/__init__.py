"""The Unite EV Charger integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

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


def _baseline_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, 1, f"{DOMAIN}_baseline_{entry.entry_id}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unite EV Charger from a config entry."""
    client = WebastoModbus(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        unit_id=entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
    )
    coordinator = WebastoCoordinator(hass, entry, client)

    try:
        await coordinator.async_read_device_info()
    except WebastoModbusError as err:
        await client.async_close()
        raise ConfigEntryNotReady(f"Could not reach the charger: {err}") from err

    coordinator.controller = ChargeControl(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Read the lockable-cable installation setting once in the background; the
    # switch stays unavailable until it is known (or when no web UI login set).
    hass.async_create_task(coordinator.async_refresh_lockable_cable())
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    async def _async_restore_on_stop(_event) -> None:
        coord: WebastoCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coord is not None:
            try:
                await coord.async_restore_baseline_on_exit()
            except Exception:  # noqa: BLE001 - shutdown must never hang on this
                _LOGGER.exception("Baseline restore on shutdown failed")

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_restore_on_stop)
    )
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
        try:
            await coordinator.async_restore_baseline_on_exit()
        except Exception:  # noqa: BLE001 - unload must still close the client
            _LOGGER.exception("Baseline restore on unload failed")
        await coordinator.client.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry; drop its stored register baseline."""
    try:
        await _baseline_store(hass, entry).async_remove()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not remove the stored register baseline", exc_info=True)
