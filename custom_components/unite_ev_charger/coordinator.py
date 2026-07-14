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
    ABS_MAX_CURRENT_A,
    CONF_DLB_ENABLED,
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_ORIGINAL_CURRENT_LIMIT,
    CONF_POLL_INTERVAL,
    CONF_TELEMETRY_REGISTER_TYPE,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TELEMETRY_REGISTER_TYPE,
    DOMAIN,
    TELEMETRY_REGISTER_AUTO,
    TELEMETRY_REGISTER_HOLDING,
    TELEMETRY_REGISTER_INPUT,
)
from .control import effective_poll_interval, normalize_failsafe_current
from .modbus import WebastoModbus, WebastoModbusError
from .models import DeviceInfo, WallboxData, apply_session, parse_telemetry
from .registers import RegType
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
        self.telemetry_register_type: str | None = None
        self.failsafe_configured: bool | None = None
        self._telemetry_preference = entry.options.get(
            CONF_TELEMETRY_REGISTER_TYPE,
            DEFAULT_TELEMETRY_REGISTER_TYPE,
        )

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

    async def async_capture_original_current_limit(self) -> int:
        """Persist register 5004 once, before this integration takes ownership."""
        stored = self.entry.data.get(CONF_ORIGINAL_CURRENT_LIMIT)
        if stored is not None:
            _LOGGER.info("Using stored original charging-current limit: %s A", stored)
            return int(stored)

        original = int(await self.client.read_register(R.SET_CURRENT_A))
        if not 0 <= original <= ABS_MAX_CURRENT_A:
            raise WebastoModbusError(
                f"Original charging-current limit {original} is outside "
                f"0..{ABS_MAX_CURRENT_A} A"
            )
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_ORIGINAL_CURRENT_LIMIT: original},
        )
        _LOGGER.info(
            "Captured original charging-current limit from register 5004: %s A",
            original,
        )
        return original

    async def async_restore_original_current_limit(self) -> bool:
        """Best-effort restoration of register 5004 before releasing ownership."""
        original = self.entry.data.get(CONF_ORIGINAL_CURRENT_LIMIT)
        if original is None:
            _LOGGER.warning(
                "Cannot restore charging-current limit: original value is missing"
            )
            return False
        try:
            await self.client.write_register(R.SET_CURRENT_A, int(original))
        except WebastoModbusError as err:
            _LOGGER.warning(
                "Could not restore original charging-current limit to %s A: %s",
                original,
                err,
            )
            return False
        _LOGGER.info(
            "Restored original charging-current limit to register 5004: %s A",
            original,
        )
        return True

    async def _async_update_data(self) -> WallboxData:
        try:
            # Claim a new connection before telemetry. This puts the charger at
            # the configured failsafe current before normal control can resume.
            if not self.client.connected:
                await self.async_claim_connection()

            telemetry = await self._read_telemetry_block()
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

            # DLB with incomplete/stale inputs deliberately withholds Alive so
            # the wallbox watchdog also enforces its configured failsafe.
            if self.controller is None or self.controller.heartbeat_allowed:
                await write_heartbeat(self.client)

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
        await self.async_claim_connection(data.phase_switch_raw)

    async def async_claim_connection(self, phase_switch_raw: int | None = None) -> None:
        """Program safety state immediately after opening a Modbus connection."""
        failsafe_current = normalize_failsafe_current(
            int(self.entry.options.get(CONF_FAILSAFE_CURRENT, DEFAULT_FAILSAFE_CURRENT_A))
        )
        self.failsafe_configured = await program_failsafe(
            self.client,
            failsafe_current_a=failsafe_current,
            failsafe_timeout_s=int(
                self.entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
            ),
            required=bool(self.entry.options.get(CONF_DLB_ENABLED, False)),
        )
        await self.client.write_register(R.SET_CURRENT_A, failsafe_current)
        self.client.take_new_connection()
        if self.controller is not None:
            await self.controller.async_on_reconnect(phase_switch_raw)

    async def _read_telemetry_block(self) -> list[int]:
        """Read telemetry using configured or auto-detected register area."""
        type_map = {
            TELEMETRY_REGISTER_INPUT: RegType.INPUT,
            TELEMETRY_REGISTER_HOLDING: RegType.HOLDING,
        }
        if self._telemetry_preference == TELEMETRY_REGISTER_AUTO:
            first = self.telemetry_register_type or TELEMETRY_REGISTER_INPUT
            second = (
                TELEMETRY_REGISTER_HOLDING
                if first == TELEMETRY_REGISTER_INPUT
                else TELEMETRY_REGISTER_INPUT
            )
            candidates = (first, second)
        else:
            candidates = (self._telemetry_preference,)

        last_error: WebastoModbusError | None = None
        for candidate in candidates:
            try:
                block = await self.client.read_block(
                    type_map[candidate],
                    R.TELEMETRY_BASE,
                    R.TELEMETRY_COUNT,
                )
            except WebastoModbusError as err:
                last_error = err
                continue
            if self.telemetry_register_type != candidate:
                _LOGGER.info("Using %s registers for charger telemetry", candidate)
            self.telemetry_register_type = candidate
            return block
        assert last_error is not None
        raise last_error

    @property
    def device_unique_id(self) -> str:
        # Must be STABLE for the life of the config entry: HA keys the device
        # and every entity on this value. `entry.unique_id` is captured once at
        # config time (serial, or host:port) and never changes. Deriving it from
        # the runtime `device.serial_number` instead caused a duplicate device on
        # restart whenever that read transiently returned nothing (charger still
        # booting / Modbus busy) and it fell back to entry_id.
        return self.entry.unique_id or self.entry.entry_id
