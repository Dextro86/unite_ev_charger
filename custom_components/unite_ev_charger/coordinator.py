"""Polling coordinator for the Unite EV Charger.

One block read for telemetry, one for the session, a heartbeat write, and -
when control is configured - one current write per cycle. That is at most a
handful of Modbus transactions every poll interval, instead of dozens.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import (
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_POLL_INTERVAL,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .control import effective_poll_interval
from .modbus import WebastoModbus, WebastoModbusError
from .models import DeviceInfo, WallboxData, apply_session, parse_telemetry
from .safety import program_failsafe, write_heartbeat

_LOGGER = logging.getLogger(__name__)


class WebastoCoordinator(DataUpdateCoordinator[WallboxData]):
    """Coordinates polling and (later) control for one wallbox."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WebastoModbus,
    ) -> None:
        self.entry = entry
        self.client = client
        self.device = DeviceInfo()
        self.controller = None  # wired in once control is built
        # monotonic deadline until which a web-UI reboot is considered in
        # progress (set by the restart button); drives the 'restarting' state.
        self.rest_restart_until: float | None = None
        # Last restart attempt (recorded by the button, no polling).
        self.rest_last_restart_at: datetime | None = None
        self.rest_last_restart_result: str | None = None  # success/auth_failed/unreachable

        poll = int(entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        ))
        failsafe_timeout = int(
            entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
        )
        # Never poll slower than the Alive heartbeat needs: the Unite drops the
        # socket + resets register 405 if Alive lapses past the failsafe timeout.
        effective_poll = effective_poll_interval(poll, failsafe_timeout)
        if effective_poll != poll:
            _LOGGER.debug(
                "Polling every %s s (configured %s s) to keep the Alive heartbeat "
                "within the %s s failsafe timeout",
                effective_poll,
                poll,
                failsafe_timeout,
            )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=effective_poll),
        )

    async def async_read_device_info(self) -> None:
        """Read static identity once. Best effort - missing fields are tolerated."""

        async def _try(reg: R.RegisterDef):
            try:
                return await self.client.read_register(reg)
            except WebastoModbusError as err:
                _LOGGER.debug("Optional identity register %s unavailable: %s", reg.name, err)
                return None

        self.device.serial_number = await _try(R.SERIAL_NUMBER)
        self.device.brand = await _try(R.BRAND)
        self.device.model = await _try(R.MODEL)
        self.device.firmware_version = await _try(R.FIRMWARE_VERSION)
        phases = await _try(R.NUMBER_OF_PHASES)
        if phases is not None:
            self.device.phases_supported = int(phases)
        min_a = await _try(R.MIN_CURRENT_HW_A)
        if min_a:
            self.device.min_current_a = int(min_a)
        max_a = await _try(R.MAX_CURRENT_CABLE_A)
        if max_a:
            self.device.max_current_a = int(max_a)

    async def _async_update_data(self) -> WallboxData:
        try:
            telemetry = await self.client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT)
            session = await self.client.read_input_block(R.SESSION_BASE, R.SESSION_COUNT)
            data = parse_telemetry(telemetry)
            apply_session(data, session)

            try:
                data.set_current_a = int(await self.client.read_register(R.SET_CURRENT_A))
            except WebastoModbusError:
                data.set_current_a = None
            try:
                data.phase_switch_raw = int(await self.client.read_register(R.PHASE_SWITCH))
            except WebastoModbusError:
                data.phase_switch_raw = None
            # Re-read the phase capability (404) every cycle: a Unite can report
            # it wrong while booting, so a single setup read could permanently
            # (until reload) disable phase switching. Self-heal here; keep the
            # last good value on a failed read.
            try:
                data.phase_capability_raw = int(await self.client.read_register(R.NUMBER_OF_PHASES))
                self.device.phases_supported = data.phase_capability_raw
            except WebastoModbusError:
                data.phase_capability_raw = self.device.phases_supported

            # First connect or a reconnect after the wallbox dropped us: the
            # Unite resets its failsafe + charging-current registers on every new
            # Modbus connection, so re-assert ownership before the heartbeat.
            if self.client.take_new_connection():
                await self._on_new_connection(data)

            # Keep the failsafe watchdog happy.
            await write_heartbeat(self.client)

            if self.controller is not None:
                await self.controller.async_apply(data)

            return data
        except WebastoModbusError as err:
            raise UpdateFailed(str(err)) from err

    async def _on_new_connection(self, data: WallboxData) -> None:
        """Run the ownership handshake the wallbox expects on a fresh connection.

        Per the Vestel spec the wallbox resets failsafe, charging current *and*
        the phase register (405 -> 404 default) on every new Modbus connection.
        So we set failsafe current/timeout immediately, then let the controller
        restore the charging current and (in evcc mode) the requested phase. The
        freshly read ``phase_switch_raw`` is the reset default, used to skip a
        needless phase write when it already matches.
        """
        await program_failsafe(
            self.client,
            failsafe_current_a=int(
                self.entry.options.get(CONF_FAILSAFE_CURRENT, DEFAULT_FAILSAFE_CURRENT_A)
            ),
            failsafe_timeout_s=int(
                self.entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
            ),
        )
        if self.controller is not None:
            await self.controller.async_on_reconnect(data.phase_switch_raw)

    @property
    def device_unique_id(self) -> str:
        return self.device.serial_number or self.entry.entry_id
