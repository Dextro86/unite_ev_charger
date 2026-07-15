"""The Unite EV Charger integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_AUTOMATIC_CONTROL,
    CONF_BASELINE_REQUIRED,
    CONF_HOST,
    CONF_ORIGINAL_CURRENT_LIMIT,
    CONF_ORIGINAL_FAILSAFE_CURRENT,
    CONF_ORIGINAL_FAILSAFE_TIMEOUT,
    CONF_ORIGINAL_PHASE_SWITCH,
    CONF_OWNERSHIP_DIRTY,
    CONF_PORT,
    CONF_UNIT_ID,
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    DOMAIN,
    ISSUE_LEGACY_BASELINE_REQUIRED,
    PLATFORMS,
)
from .controller import ChargeControl
from .coordinator import WebastoCoordinator
from .modbus import WebastoModbus, WebastoModbusError

_LOGGER = logging.getLogger(__name__)

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Prevent legacy EMS values from being mistaken for proven originals."""
    if entry.version >= 2:
        return True

    data = dict(entry.data)
    for key in (
        CONF_ORIGINAL_CURRENT_LIMIT,
        CONF_ORIGINAL_FAILSAFE_CURRENT,
        CONF_ORIGINAL_FAILSAFE_TIMEOUT,
        CONF_ORIGINAL_PHASE_SWITCH,
    ):
        data.pop(key, None)
    data.update(
        {
            CONF_AUTOMATIC_CONTROL: False,
            CONF_OWNERSHIP_DIRTY: False,
            CONF_BASELINE_REQUIRED: True,
        }
    )
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    _LOGGER.warning(
        "Legacy entry migrated with automatic control disabled: original "
        "failsafe registers were never captured and must not be guessed"
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unite EV Charger from a config entry."""
    client = WebastoModbus(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        unit_id=entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
    )
    coordinator = WebastoCoordinator(hass, entry, client)
    await coordinator.async_load_ownership_record()
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
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        if entry.data.get(CONF_BASELINE_REQUIRED):
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{ISSUE_LEGACY_BASELINE_REQUIRED}_{entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_LEGACY_BASELINE_REQUIRED,
            )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError) as err:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator.ownership_dirty:
            try:
                restored = await coordinator.async_suspend(preserve_requested=True)
            except (Exception, asyncio.CancelledError):
                _LOGGER.critical(
                    "Setup failed and charger restoration also failed; "
                    "ownership journal retained",
                    exc_info=True,
                )
            else:
                if not restored:
                    _LOGGER.critical(
                        "Setup failed and charger restoration also failed; "
                        "ownership journal retained"
                    )
        if isinstance(err, WebastoModbusError):
            raise ConfigEntryNotReady(f"Could not reach the charger: {err}") from err
        raise

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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only recovery data proven clean before config-entry deletion."""
    domain_data = hass.data.get(DOMAIN, {})
    coordinator: WebastoCoordinator | None = domain_data.pop(entry.entry_id, None)
    retained = coordinator is not None
    if coordinator is None:
        client = WebastoModbus(
            host=entry.data[CONF_HOST],
            port=entry.data.get(CONF_PORT, DEFAULT_PORT),
            unit_id=entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
        )
        coordinator = WebastoCoordinator(hass, entry, client)

    if coordinator.ownership_dirty:
        if not retained:
            _LOGGER.critical(
                "Config entry removed with a dirty EMS ownership journal; "
                "manual charger recovery required and journal retained"
            )
            return
        try:
            restored = await coordinator.async_suspend(preserve_requested=True)
        except asyncio.CancelledError:
            _LOGGER.critical(
                "Removal cancelled before charger restoration; manual charger "
                "recovery required and journal retained"
            )
            raise
        except Exception:
            _LOGGER.critical(
                "Charger restoration raised during removal; manual charger "
                "recovery required and journal retained",
                exc_info=True,
            )
            return
        if not restored:
            _LOGGER.critical(
                "Charger restoration failed during removal; manual charger "
                "recovery required and journal retained"
            )
            return

    await coordinator.async_remove_clean_ownership_record()
