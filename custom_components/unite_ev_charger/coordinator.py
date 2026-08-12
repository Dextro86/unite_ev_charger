"""Polling coordinator for the Unite EV Charger.

One block read for telemetry, one for the session, a heartbeat write, and -
when control is configured - one current write per cycle. That is at most a
handful of Modbus transactions every poll interval, instead of dozens.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import (
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_GRID_PHASES,
    CONF_HOST,
    CONF_PHASE_RESTORE_ON_UNPLUG,
    CONF_POLL_INTERVAL,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_PHASE_RESTORE_ON_UNPLUG,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DOMAIN,
)
from . import control as ctrl
from .control import effective_poll_interval
from .rest_client import UniteRestError, async_restore_three_phase
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
        self._vehicle_was_connected = False
        self._auto_restore_task: asyncio.Task | None = None
        self.last_auto_phase_restore: datetime | None = None
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
            # Session RFID tag: only meaningful while a vehicle is connected, and
            # absent on firmware older than spec v1.9 - so it is gated and its own
            # failure never affects the rest of the cycle.
            if data.vehicle_connected:
                try:
                    tag = str(await self.client.read_register(R.SESSION_RFID_TAG)).strip()
                    data.session_rfid = tag or None
                except WebastoModbusError:
                    data.session_rfid = None

            self._maybe_auto_restore_phase(data)

            # First connect or a reconnect after the wallbox dropped us: the
            # Unite resets its failsafe + charging-current registers on every new
            # Modbus connection, so re-assert ownership before the heartbeat.
            if self.client.take_new_connection():
                await self._on_new_connection(data)

            # Keep the failsafe watchdog happy.
            await write_heartbeat(self.client)

            if self.controller is not None:
                try:
                    await self.controller.async_apply(data)
                except WebastoModbusError:
                    # A genuine Modbus failure: let it reach the outer handler so
                    # the connection is dropped and reclaimed next cycle.
                    raise
                except Exception:  # noqa: BLE001
                    # A control-logic hiccup (e.g. a stale/malformed meter sensor)
                    # must never blank out monitoring. Log it and keep serving the
                    # telemetry we already read; control retries next cycle.
                    _LOGGER.exception(
                        "Charge control step failed; keeping monitoring alive"
                    )

            return data
        except WebastoModbusError as err:
            raise UpdateFailed(str(err)) from err

    def _maybe_auto_restore_phase(self, data: WallboxData) -> None:
        """Re-sync a stuck 1-phase config at unplug, if the user opted in.

        The web-UI toggle that fixes this breaks the running charging session, so
        the only free moment is right after the vehicle is unplugged: the next
        plug-in then starts with the config already correct. Only fires when the
        charger really is stuck (register 404 reads 0) on an installation the
        user declared as 3-phase - on a genuine 1-phase wallbox 404 = 0 is
        correct and must be left alone.
        """
        connected = data.vehicle_connected
        just_unplugged = self._vehicle_was_connected and not connected
        self._vehicle_was_connected = connected
        if not just_unplugged:
            return
        o = self.entry.options
        if not o.get(CONF_PHASE_RESTORE_ON_UNPLUG, DEFAULT_PHASE_RESTORE_ON_UNPLUG):
            return
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            return
        if data.phase_capability_raw != 0:
            return  # not stuck
        if not ctrl.is_three_phase_install(o.get(CONF_GRID_PHASES), data.phase_capability_raw):
            return  # genuinely 1-phase (or unanswered) -> never write a 3-phase config
        if self._auto_restore_task is not None and not self._auto_restore_task.done():
            return
        self._auto_restore_task = self.hass.async_create_task(self._async_auto_restore_phase())

    async def _async_auto_restore_phase(self) -> None:
        o = self.entry.options
        try:
            route = await async_restore_three_phase(
                async_get_clientsession(self.hass),
                self.entry.data[CONF_HOST],
                o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                o.get(CONF_REST_PASSWORD, ""),
            )
        except UniteRestError as err:
            _LOGGER.warning(
                "Charger is stuck on 1-phase, but the automatic restore failed: %s", err
            )
            return
        self.last_auto_phase_restore = datetime.now(timezone.utc)
        _LOGGER.info(
            "Vehicle unplugged with the charger stuck on 1-phase; restored the "
            "3-phase config via %s",
            route,
        )
        await self.async_request_refresh()

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
        # Must be STABLE for the life of the config entry: HA keys the device
        # and every entity on this value. `entry.unique_id` is captured once at
        # config time (serial, or host:port) and never changes. Deriving it from
        # the runtime `device.serial_number` instead caused a duplicate device on
        # restart whenever that read transiently returned nothing (charger still
        # booting / Modbus busy) and it fell back to entry_id.
        return self.entry.unique_id or self.entry.entry_id
