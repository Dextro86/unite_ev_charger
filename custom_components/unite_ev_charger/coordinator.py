"""Polling coordinator for the Unite EV Charger.

One block read for telemetry, one for the session, a heartbeat write, and -
when control is configured - one current write per cycle. That is at most a
handful of Modbus transactions every poll interval, instead of dozens.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import (
    ABS_MAX_CURRENT_A,
    CONF_AUTOMATIC_CONTROL,
    CONF_BASELINE_REQUIRED,
    CONF_CONTROL_MODE,
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_HOST,
    CONF_ORIGINAL_FAILSAFE_CURRENT,
    CONF_ORIGINAL_FAILSAFE_TIMEOUT,
    CONF_ORIGINAL_CURRENT_LIMIT,
    CONF_ORIGINAL_PHASE_SWITCH,
    CONF_OWNERSHIP_DIRTY,
    CONF_PHASE_SWITCHING,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_TELEMETRY_REGISTER_TYPE,
    CONF_UNIT_ID,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    CONTROL_EXTERNAL,
    DEFAULT_PORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TELEMETRY_REGISTER_TYPE,
    DEFAULT_UNIT_ID,
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


class OwnershipState(str, Enum):
    """Runtime EMS ownership lifecycle."""

    DISABLED = "disabled"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OriginalChargerConfig:
    """Charger values captured before one EMS ownership session."""

    current_limit: int
    failsafe_current: int
    failsafe_timeout: int
    phase_switch: int | None = None


_SNAPSHOT_KEYS = (
    CONF_ORIGINAL_CURRENT_LIMIT,
    CONF_ORIGINAL_FAILSAFE_CURRENT,
    CONF_ORIGINAL_FAILSAFE_TIMEOUT,
    CONF_ORIGINAL_PHASE_SWITCH,
)
_OWNERSHIP_KEYS = (
    CONF_AUTOMATIC_CONTROL,
    CONF_OWNERSHIP_DIRTY,
    *_SNAPSHOT_KEYS,
)
_OWNERSHIP_STORE_VERSION = 1


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
        self._ownership_lock = asyncio.Lock()
        self._ownership_store = Store(
            hass,
            _OWNERSHIP_STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.ownership",
        )
        self._ownership_state = (
            OwnershipState.ERROR
            if entry.data.get(CONF_OWNERSHIP_DIRTY)
            else OwnershipState.DISABLED
        )
        self._loaded_options = dict(entry.options)
        self._loaded_connection = (
            entry.data.get(CONF_HOST),
            entry.data.get(CONF_PORT, DEFAULT_PORT),
            entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
        )
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

    def _ensure_ownership_runtime(self) -> None:
        """Initialise lifecycle fields for normal and lightweight test instances."""
        if not hasattr(self, "_ownership_lock"):
            self._ownership_lock = asyncio.Lock()
        if not hasattr(self, "_ownership_state"):
            self._ownership_state = (
                OwnershipState.ERROR
                if self.entry.data.get(CONF_OWNERSHIP_DIRTY)
                else OwnershipState.DISABLED
            )

    @property
    def ownership_state(self) -> OwnershipState:
        self._ensure_ownership_runtime()
        return self._ownership_state

    @property
    def automatic_control_requested(self) -> bool:
        return bool(self.entry.data.get(CONF_AUTOMATIC_CONTROL, True))

    @property
    def ownership_dirty(self) -> bool:
        return bool(self.entry.data.get(CONF_OWNERSHIP_DIRTY, False))

    @property
    def ownership_active(self) -> bool:
        return self.ownership_state is OwnershipState.ACTIVE

    @property
    def original_configuration(self) -> OriginalChargerConfig | None:
        data = self.entry.data
        required = (
            CONF_ORIGINAL_CURRENT_LIMIT,
            CONF_ORIGINAL_FAILSAFE_CURRENT,
            CONF_ORIGINAL_FAILSAFE_TIMEOUT,
        )
        if not self.ownership_dirty or any(key not in data for key in required):
            return None
        phase = data.get(CONF_ORIGINAL_PHASE_SWITCH)
        return OriginalChargerConfig(
            current_limit=int(data[CONF_ORIGINAL_CURRENT_LIMIT]),
            failsafe_current=int(data[CONF_ORIGINAL_FAILSAFE_CURRENT]),
            failsafe_timeout=int(data[CONF_ORIGINAL_FAILSAFE_TIMEOUT]),
            phase_switch=None if phase is None else int(phase),
        )

    def _set_ownership_state(self, state: OwnershipState, reason: str) -> None:
        previous = self.ownership_state
        if previous is state:
            return
        self._ownership_state = state
        _LOGGER.info(
            "EMS ownership state %s -> %s: %s",
            previous.value,
            state.value,
            reason,
        )
        update_listeners = getattr(self, "async_update_listeners", None)
        if callable(update_listeners):
            update_listeners()

    @staticmethod
    def _ownership_record(data: dict) -> dict[str, object]:
        record: dict[str, object] = {
            CONF_AUTOMATIC_CONTROL: bool(data.get(CONF_AUTOMATIC_CONTROL, True)),
            CONF_OWNERSHIP_DIRTY: bool(data.get(CONF_OWNERSHIP_DIRTY, False)),
        }
        if record[CONF_OWNERSHIP_DIRTY]:
            for key in _SNAPSHOT_KEYS:
                if key in data:
                    record[key] = data[key]
        return record

    async def async_load_ownership_record(self) -> None:
        """Load the durable journal before setup decides whether to connect."""
        store = getattr(self, "_ownership_store", None)
        if store is None:
            return
        record = await store.async_load()
        if not isinstance(record, dict):
            if any(key in self.entry.data for key in _OWNERSHIP_KEYS):
                await store.async_save(self._ownership_record(self.entry.data))
            return
        data = dict(self.entry.data)
        for key in _OWNERSHIP_KEYS:
            data.pop(key, None)
        data.update({key: record[key] for key in _OWNERSHIP_KEYS if key in record})
        if data != self.entry.data:
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        self._ownership_state = (
            OwnershipState.ERROR
            if data.get(CONF_OWNERSHIP_DIRTY)
            else OwnershipState.DISABLED
        )

    async def _persist_ownership_data(
        self,
        updates: dict[str, object],
        *,
        remove: tuple[str, ...] = (),
    ) -> None:
        data = dict(self.entry.data)
        data.update(updates)
        for key in remove:
            data.pop(key, None)
        store = getattr(self, "_ownership_store", None)
        if store is not None:
            await store.async_save(self._ownership_record(data))
        if data == self.entry.data:
            return
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _phase_may_be_owned(self) -> bool:
        options = self.entry.options
        return bool(options.get(CONF_PHASE_SWITCHING, False)) or (
            options.get(CONF_CONTROL_MODE) == CONTROL_EXTERNAL
        )

    def configuration_changed(self, entry: ConfigEntry) -> bool:
        """Ignore ownership persistence; reload only user configuration changes."""
        connection = (
            entry.data.get(CONF_HOST),
            entry.data.get(CONF_PORT, DEFAULT_PORT),
            entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
        )
        return connection != self._loaded_connection or dict(entry.options) != self._loaded_options

    async def _async_capture_original_configuration(self) -> OriginalChargerConfig:
        """Read and durably persist one ownership-session baseline."""
        current = int(await self.client.read_register(R.SET_CURRENT_A))
        failsafe_current = int(await self.client.read_register(R.FAILSAFE_CURRENT_A))
        failsafe_timeout = int(await self.client.read_register(R.FAILSAFE_TIMEOUT_S))
        phase = (
            int(await self.client.read_register(R.PHASE_SWITCH))
            if self._phase_may_be_owned()
            else None
        )

        if not 0 <= current <= ABS_MAX_CURRENT_A:
            raise WebastoModbusError(
                f"Original charging-current limit {current} is outside "
                f"0..{ABS_MAX_CURRENT_A} A"
            )
        if not 0 <= failsafe_current <= ABS_MAX_CURRENT_A:
            raise WebastoModbusError(
                f"Original failsafe current {failsafe_current} is outside "
                f"0..{ABS_MAX_CURRENT_A} A"
            )
        if not 0 <= failsafe_timeout <= 0xFFFF:
            raise WebastoModbusError(
                f"Original failsafe timeout {failsafe_timeout} is outside 0..65535 s"
            )
        if phase is not None and phase not in (0, 1):
            raise WebastoModbusError(
                f"Original phase selection {phase} is outside 0..1"
            )

        original = OriginalChargerConfig(
            current_limit=current,
            failsafe_current=failsafe_current,
            failsafe_timeout=failsafe_timeout,
            phase_switch=phase,
        )
        updates: dict[str, object] = {
            CONF_AUTOMATIC_CONTROL: True,
            CONF_OWNERSHIP_DIRTY: True,
            CONF_ORIGINAL_CURRENT_LIMIT: current,
            CONF_ORIGINAL_FAILSAFE_CURRENT: failsafe_current,
            CONF_ORIGINAL_FAILSAFE_TIMEOUT: failsafe_timeout,
        }
        if phase is not None:
            updates[CONF_ORIGINAL_PHASE_SWITCH] = phase
        await self._persist_ownership_data(
            updates,
            remove=(CONF_BASELINE_REQUIRED,),
        )
        _LOGGER.info(
            "Captured original charger configuration: register 5004=%s A, "
            "2000=%s A, 2002=%s s, 405=%s",
            current,
            failsafe_current,
            failsafe_timeout,
            phase if phase is not None else "not owned",
        )
        return original

    async def async_activate(self) -> None:
        """Capture a lease and claim EMS control exactly once."""
        self._ensure_ownership_runtime()
        async with self._ownership_lock:
            if self.ownership_state is OwnershipState.ACTIVE:
                return
            self._set_ownership_state(OwnershipState.INITIALIZING, "control requested")
            try:
                original = self.original_configuration
                if self.ownership_dirty and original is None:
                    raise WebastoModbusError(
                        "Dirty EMS ownership record is incomplete; refusing to recapture"
                    )
                if original is None:
                    original = await self._async_capture_original_configuration()
                else:
                    await self._persist_ownership_data(
                        {CONF_AUTOMATIC_CONTROL: True},
                        remove=(CONF_BASELINE_REQUIRED,),
                    )
                    _LOGGER.info(
                        "Resuming EMS ownership with stored originals: "
                        "5004=%s A, 2000=%s A, 2002=%s s, 405=%s",
                        original.current_limit,
                        original.failsafe_current,
                        original.failsafe_timeout,
                        original.phase_switch,
                    )
                await self.async_claim_connection()
            except WebastoModbusError:
                if self.ownership_dirty:
                    await self._async_restore_locked(preserve_requested=True)
                else:
                    self._set_ownership_state(OwnershipState.ERROR, "activation failed")
                    await self.client.async_close()
                raise
            self._set_ownership_state(OwnershipState.ACTIVE, "control handshake complete")

    async def async_suspend(self, *, preserve_requested: bool) -> bool:
        """Restore the current lease before releasing EMS ownership."""
        self._ensure_ownership_runtime()
        async with self._ownership_lock:
            return await self._async_restore_locked(
                preserve_requested=preserve_requested
            )

    async def _async_restore_locked(self, *, preserve_requested: bool) -> bool:
        if not preserve_requested and self.automatic_control_requested:
            await self._persist_ownership_data({CONF_AUTOMATIC_CONTROL: False})

        original = self.original_configuration
        if original is None:
            if getattr(self.client, "connected", False):
                await self.client.async_close()
            self._set_ownership_state(OwnershipState.SUSPENDED, "no active lease")
            return True

        self._set_ownership_state(OwnershipState.SUSPENDING, "restoring charger")
        if self.controller is not None:
            await self.controller.async_shutdown()

        registers: list[tuple[R.RegisterDef, int]] = [
            (R.SET_CURRENT_A, original.current_limit),
            (R.FAILSAFE_CURRENT_A, original.failsafe_current),
            (R.FAILSAFE_TIMEOUT_S, original.failsafe_timeout),
        ]
        if original.phase_switch is not None:
            registers.append((R.PHASE_SWITCH, original.phase_switch))

        try:
            await write_heartbeat(self.client)
            for register, value in registers:
                await self.client.write_register(register, value)
                _LOGGER.info(
                    "Restored original register %s (%s) to %s",
                    register.address,
                    register.name,
                    value,
                )
            for register, expected in registers:
                actual = int(await self.client.read_register(register))
                if actual != expected:
                    raise WebastoModbusError(
                        f"Restore verification failed for register {register.address}: "
                        f"expected {expected}, got {actual}"
                    )
                _LOGGER.info(
                    "Verified original register %s (%s): %s",
                    register.address,
                    register.name,
                    actual,
                )
        except WebastoModbusError as err:
            self._set_ownership_state(OwnershipState.ERROR, "restoration failed")
            _LOGGER.error(
                "Could not release EMS ownership; retaining originals "
                "5004=%s A, 2000=%s A, 2002=%s s, 405=%s: %s",
                original.current_limit,
                original.failsafe_current,
                original.failsafe_timeout,
                original.phase_switch,
                err,
            )
            return False

        await self.client.async_close()
        await self._persist_ownership_data(
            {CONF_OWNERSHIP_DIRTY: False},
            remove=_SNAPSHOT_KEYS,
        )
        self._set_ownership_state(OwnershipState.SUSPENDED, "charger restored")
        return True

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
        if not self.ownership_active:
            if self.automatic_control_requested:
                await self.async_activate()
            elif self.ownership_dirty:
                await self.async_suspend(preserve_requested=True)
            if not self.ownership_active:
                raise UpdateFailed("Automatic charger control is not active")
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
            self._set_ownership_state(
                OwnershipState.ERROR,
                "Modbus communication failed while control was active",
            )
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
        failsafe_timeout = int(
            self.entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
        )
        self.failsafe_configured = await program_failsafe(
            self.client,
            failsafe_current_a=failsafe_current,
            failsafe_timeout_s=failsafe_timeout,
        )
        await self.client.write_register(R.SET_CURRENT_A, failsafe_current)
        _LOGGER.info(
            "Claimed charger control: live limit=%s A, failsafe=%s A after %s s "
            "(registers configured=%s)",
            failsafe_current,
            failsafe_current,
            failsafe_timeout,
            self.failsafe_configured,
        )
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
