"""HA-aware charging controller.

Holds the runtime intent (charging on/off, mode, manual current) and the
stateful bits (surplus smoothing, anti-short-cycle, last setpoint), reads the
external sensors, calls the pure logic in control.py, and writes one setpoint
per cycle.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import control as ctrl
from . import registers as R
from .const import (
    ABS_MAX_CURRENT_A,
    CONF_CONTROL_MODE,
    CONF_DEFAULT_MODE,
    CONF_DLB_CURRENT_L1,
    CONF_DLB_CURRENT_L2,
    CONF_DLB_CURRENT_L3,
    CONF_DLB_ENABLED,
    CONF_DLB_MARGIN_A,
    CONF_DLB_PHASES,
    CONF_DLB_SENSOR_MAX_AGE,
    CONF_EXPORT_SENSOR,
    CONF_GRID_EXPORT_NEGATIVE,
    CONF_GRID_POWER_SENSOR,
    CONF_FAILSAFE_CURRENT,
    CONF_INCREASE_DELAY,
    CONF_INCREASE_STEP,
    CONF_IMPORT_SENSOR,
    CONF_MAIN_FUSE_A,
    CONF_MAX_CURRENT,
    CONF_METER_MODEL,
    CONF_MIN_CURRENT,
    CONF_NOMINAL_VOLTAGE,
    CONF_PHASE_RECOVERY_DWELL,
    CONF_PHASE_RECOVERY_ENABLED,
    CONF_PHASE_RECOVERY_OBSERVE,
    CONF_PHASE_SWITCH_DWELL,
    CONF_PHASE_SWITCHING,
    CONF_RESET_ON_DISCONNECT,
    CONF_SOLAR_MIN_CURRENT,
    CONF_SURPLUS_SENSOR,
    CONTROL_EXTERNAL,
    DEFAULT_CONTROL_MODE,
    DEFAULT_DLB_MARGIN_A,
    DEFAULT_DLB_PHASES,
    DEFAULT_DLB_SENSOR_MAX_AGE_S,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_INCREASE_DELAY_S,
    DEFAULT_INCREASE_STEP_A,
    DEFAULT_MAIN_FUSE_A,
    DEFAULT_MAX_CURRENT_A,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_MODE,
    DEFAULT_PHASE_RECOVERY_DWELL_S,
    DEFAULT_PHASE_RECOVERY_ENABLED,
    DEFAULT_PHASE_RECOVERY_OBSERVE_S,
    DEFAULT_PHASE_SWITCH_DWELL_S,
    DEFAULT_PHASE_PREFERENCE,
    DEFAULT_PHASE_SWITCHING,
    DLB_CURRENT_PLAUSIBLE_FLOOR_A,
    METER_DSMR,
    METER_NONE,
    METER_SIGNED_GRID,
    METER_SURPLUS,
    NOMINAL_VOLTAGE,
    PHASE_1P,
    PHASE_3P,
    PHASE_AUTO,
    PHASE_MEASURE_OFF_A,
    PHASE_MEASURE_ON_A,
    PHASE_RECOVERY_SETTLE_S,
    PHASE_SWITCH_QUIET_S,
    PHASE_SWITCH_SETTLE_MIN_S,
    RECOVERY_ABORTED,
    RECOVERY_COMPLETE,
    RECOVERY_DWELLING,
    RECOVERY_IDLE,
    RECOVERY_OBSERVING,
    RECOVERY_RESUMING,
    SOLAR_MIN_CHARGE_DURATION_S,
    SOLAR_MODES,
    SOLAR_SMOOTHING_S,
)
from .inputs import read_current_a, read_power_w
from .modbus import WebastoModbusError
from .models import WallboxData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlConfig:
    control_mode: str
    default_mode: str
    min_current: int
    max_current: int
    reset_on_disconnect: bool
    nominal_voltage: int
    meter_model: str
    grid_power_sensor: str | None
    grid_export_negative: bool
    import_sensor: str | None
    export_sensor: str | None
    surplus_sensor: str | None
    solar_min_current: int
    dlb_enabled: bool
    main_fuse_a: int
    dlb_margin_a: int
    dlb_phases: int
    dlb_sensor_max_age: int
    dlb_l1: str | None
    dlb_l2: str | None
    dlb_l3: str | None
    phase_switching: bool
    phase_switch_dwell: int
    phase_recovery_enabled: bool
    phase_recovery_observe: int
    phase_recovery_dwell: int
    failsafe_current: int
    increase_delay: int
    increase_step: int

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> "ControlConfig":
        o = entry.options
        return cls(
            control_mode=o.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE),
            default_mode=o.get(CONF_DEFAULT_MODE, DEFAULT_MODE),
            min_current=int(o.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT_A)),
            max_current=int(o.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT_A)),
            reset_on_disconnect=bool(o.get(CONF_RESET_ON_DISCONNECT, True)),
            nominal_voltage=int(o.get(CONF_NOMINAL_VOLTAGE, NOMINAL_VOLTAGE)),
            meter_model=o.get(CONF_METER_MODEL, METER_NONE),
            grid_power_sensor=o.get(CONF_GRID_POWER_SENSOR),
            grid_export_negative=bool(o.get(CONF_GRID_EXPORT_NEGATIVE, True)),
            import_sensor=o.get(CONF_IMPORT_SENSOR),
            export_sensor=o.get(CONF_EXPORT_SENSOR),
            surplus_sensor=o.get(CONF_SURPLUS_SENSOR),
            solar_min_current=int(o.get(CONF_SOLAR_MIN_CURRENT, DEFAULT_MIN_CURRENT_A)),
            dlb_enabled=bool(o.get(CONF_DLB_ENABLED, False)),
            main_fuse_a=int(o.get(CONF_MAIN_FUSE_A, DEFAULT_MAIN_FUSE_A)),
            dlb_margin_a=int(o.get(CONF_DLB_MARGIN_A, DEFAULT_DLB_MARGIN_A)),
            dlb_phases=int(o.get(CONF_DLB_PHASES, DEFAULT_DLB_PHASES)),
            dlb_sensor_max_age=int(
                o.get(CONF_DLB_SENSOR_MAX_AGE, DEFAULT_DLB_SENSOR_MAX_AGE_S)
            ),
            dlb_l1=o.get(CONF_DLB_CURRENT_L1),
            dlb_l2=o.get(CONF_DLB_CURRENT_L2),
            dlb_l3=o.get(CONF_DLB_CURRENT_L3),
            phase_switching=bool(o.get(CONF_PHASE_SWITCHING, DEFAULT_PHASE_SWITCHING)),
            phase_switch_dwell=int(o.get(CONF_PHASE_SWITCH_DWELL, DEFAULT_PHASE_SWITCH_DWELL_S)),
            phase_recovery_enabled=bool(
                o.get(CONF_PHASE_RECOVERY_ENABLED, DEFAULT_PHASE_RECOVERY_ENABLED)
            ),
            phase_recovery_observe=int(
                o.get(CONF_PHASE_RECOVERY_OBSERVE, DEFAULT_PHASE_RECOVERY_OBSERVE_S)
            ),
            phase_recovery_dwell=int(
                o.get(CONF_PHASE_RECOVERY_DWELL, DEFAULT_PHASE_RECOVERY_DWELL_S)
            ),
            failsafe_current=ctrl.normalize_failsafe_current(
                int(o.get(CONF_FAILSAFE_CURRENT, DEFAULT_FAILSAFE_CURRENT_A))
            ),
            increase_delay=int(o.get(CONF_INCREASE_DELAY, DEFAULT_INCREASE_DELAY_S)),
            increase_step=int(o.get(CONF_INCREASE_STEP, DEFAULT_INCREASE_STEP_A)),
        )


class ChargeControl:
    """Runtime intent + per-cycle control application."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.cfg = ControlConfig.from_entry(entry)

        # User intent (reflected/edited by the control entities).
        self.charging_enabled: bool = True
        self.mode: str = self.cfg.default_mode
        self.manual_current: int = self.cfg.min_current
        self.phase_preference: str = DEFAULT_PHASE_PREFERENCE

        # Exposed for sensors/diagnostics.
        self.computed_setpoint: int | None = None
        self.available_surplus_w: float | None = None
        self.dlb_healthy: bool | None = None
        self.dlb_failure_reason: str | None = None

        # Internal state.
        self._surplus_window: deque[tuple[float, float]] = deque()
        self._last_setpoint: int | None = None
        self._started_at = datetime.now(timezone.utc)
        self._increase_since: float | None = None
        self._heartbeat_allowed = not self.cfg.dlb_enabled
        self._was_connected: bool = False
        self._charge_started: float | None = None
        # Phase-switch timers.
        self._phase_diff_since: float | None = None
        self._last_switch: float | None = None
        # Current to resume to when re-enabled in external mode.
        self._ext_resume_current: int = self.cfg.max_current

        # Optional adaptive 1->3 phase recovery.
        self._recovery_task: asyncio.Task | None = None
        self._recovery_status: str = RECOVERY_IDLE
        self._recovery_remaining_s: int = 0
        self._recovery_attempted: bool = False   # latch: one escalation per 3P request
        self._buffer_commands: bool = False       # hold evcc's writes during the pause
        self._last_recovery_at: datetime | None = None
        self._last_recovery_result: str | None = None
        # evcc-facing intent (what evcc last asked). The entities report this so
        # evcc stays "in sync" even while the hardware is briefly forced to 0 A.
        self._requested_phase: str | None = None
        self._current_intent: int | None = None
        self._enabled_intent: bool | None = None

    # -- helpers used by entities -------------------------------------------
    @property
    def is_external(self) -> bool:
        return self.cfg.control_mode == CONTROL_EXTERNAL

    @property
    def recovery_active(self) -> bool:
        return self._recovery_task is not None and not self._recovery_task.done()

    @property
    def heartbeat_allowed(self) -> bool:
        """Whether current control has a trustworthy input snapshot."""
        return self._heartbeat_allowed

    @property
    def recovery_status(self) -> str:
        return self._recovery_status

    @property
    def recovery_remaining_s(self) -> int:
        return self._recovery_remaining_s

    @property
    def last_recovery_at(self) -> datetime | None:
        return self._last_recovery_at

    @property
    def last_recovery_result(self) -> str | None:
        return self._last_recovery_result

    # evcc-facing intent, shown by the switch/number/select in external mode.
    @property
    def ext_enabled_intent(self) -> bool | None:
        return self._enabled_intent

    @property
    def ext_current_intent(self) -> int | None:
        return self._current_intent

    @property
    def requested_phase(self) -> str | None:
        return self._requested_phase

    @property
    def limits(self) -> ctrl.Limits:
        return ctrl.Limits(
            min_current=self.cfg.min_current,
            max_current=self.cfg.max_current,
            cable_max=self.coordinator.device.max_current_a,
        )

    def _require_active_ownership(self) -> None:
        if getattr(self.coordinator, "ownership_active", True) is not True:
            raise WebastoModbusError(
                "EMS ownership is not active; charger write rejected"
            )

    # -- external (evcc) direct writes --------------------------------------
    # Faithful passthrough: evcc owns phase/current/enable, each command is one
    # register write - like evcc's own Vestel driver. The only exception is the
    # opt-in 1->3 recovery: while its forced pause runs, evcc's current/enable
    # commands are buffered (a 0 A / stop still passes straight through) and the
    # entities report evcc's intent so evcc stays in sync.
    async def async_external_set_current(self, amps: float) -> None:
        self._require_active_ownership()
        value = max(0, min(ABS_MAX_CURRENT_A, int(round(amps))))  # physical clamp only
        self._current_intent = value
        self._enabled_intent = value > 0
        if value > 0:
            self._ext_resume_current = value
        self.coordinator.async_update_listeners()
        if self._buffer_commands:
            # Recovery owns the setpoint. Buffer a positive current (applied on
            # resume); a stop always goes through so evcc can always halt.
            if value == 0:
                await self.coordinator.async_write_owned(R.SET_CURRENT_A, 0)
            return
        await self.coordinator.async_write_owned(R.SET_CURRENT_A, value)
        await self.coordinator.async_request_refresh()

    async def async_external_set_enabled(self, enabled: bool) -> None:
        await self.async_external_set_current(self._ext_resume_current if enabled else 0)

    async def async_external_set_phase(self, phases: int) -> None:
        self._require_active_ownership()
        if phases != 3:
            self._requested_phase = "1"
            self._recovery_attempted = False   # a 1P request re-arms recovery
            self._cancel_recovery()
            self.coordinator.async_update_listeners()
            await self._write_phase(1)
            await self.coordinator.async_request_refresh()
            return
        # phases == 3: live write first, then adaptive recovery if it didn't take.
        self._requested_phase = "3"
        self.coordinator.async_update_listeners()
        await self._write_phase(3)
        await self.coordinator.async_request_refresh()
        if self._should_start_recovery(self.coordinator.data):
            self._start_recovery()

    # -- optional adaptive 1->3 phase recovery ------------------------------
    @staticmethod
    def _measured_single_phase(data: WallboxData) -> bool:
        return (
            data.current_l1_a >= PHASE_MEASURE_ON_A
            and data.current_l2_a < PHASE_MEASURE_OFF_A
            and data.current_l3_a < PHASE_MEASURE_OFF_A
        )

    @staticmethod
    def _measured_three_phase(data: WallboxData) -> bool:
        return (
            data.current_l1_a >= PHASE_MEASURE_ON_A
            and data.current_l2_a >= PHASE_MEASURE_ON_A
            and data.current_l3_a >= PHASE_MEASURE_ON_A
        )

    def _should_start_recovery(self, data: WallboxData | None) -> bool:
        """Recovery only when it is enabled, not already tried this request, and
        the car is genuinely charging on a single phase."""
        if not self.cfg.phase_recovery_enabled:
            return False
        if self.recovery_active or self._recovery_attempted or data is None:
            return False
        if not data.vehicle_connected or not data.charging:
            return False
        return data.phase_switch_raw == 0 or self._measured_single_phase(data)

    def _start_recovery(self) -> None:
        if self.recovery_active:
            return
        self._recovery_task = asyncio.create_task(self._recovery_sequence())

    def _cancel_recovery(self) -> None:
        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._buffer_commands = False

    def _set_recovery(self, status: str, remaining_s: int = 0) -> None:
        self._recovery_status = status
        self._recovery_remaining_s = max(0, remaining_s)
        self.coordinator.async_update_listeners()

    async def async_on_reconnect(self, phase_raw: int | None) -> None:
        """Re-assert charging current AND phase after a fresh Modbus connection.

        Per the Vestel spec the wallbox resets register 405 (phase) to its 404
        default *and* its charging-current register on every Modbus disconnection,
        so we restore both instead of silently running on the wallbox defaults.
        """
        await self._reassert_current_on_reconnect()
        await self._reassert_phase_on_reconnect(phase_raw)

    async def _reassert_current_on_reconnect(self) -> None:
        if self._buffer_commands:
            # A recovery pause is in progress: keep 0 A so it is not undone.
            await self.coordinator.client.write_register(R.SET_CURRENT_A, 0)
            return
        if self.is_external:
            # evcc owns it: restore its last intent, or 0 until it commands
            # (never start charging on our own after a reconnect).
            if self._enabled_intent is False or self._current_intent is None:
                value = 0
            else:
                value = self._current_intent
            value = max(0, min(ABS_MAX_CURRENT_A, int(value)))
            await self.coordinator.client.write_register(R.SET_CURRENT_A, value)
        else:
            # Internal: the cached last-setpoint is now stale; force the control
            # loop to re-write the freshly computed setpoint this cycle.
            self._last_setpoint = None

    async def _reassert_phase_on_reconnect(self, phase_raw: int | None) -> None:
        """Restore the desired phase: the wallbox reset register 405 to its 404
        default on the disconnect (per spec).

        Internal mode's control loop re-evaluates the phase this same cycle, so
        only the external (evcc) path needs an explicit re-assert - and only when
        the reset default actually differs from evcc's request, to avoid a
        needless CP interruption.
        """
        if not self.is_external or self._requested_phase not in (PHASE_1P, PHASE_3P):
            return
        desired_raw = 1 if self._requested_phase == PHASE_3P else 0
        if phase_raw == desired_raw:
            return  # reset default already matches -> no write, no CP blip
        await self.coordinator.client.write_register(R.PHASE_SWITCH, desired_raw)

    async def async_shutdown(self) -> None:
        """Cancel any running recovery so unload/reload leaves nothing behind."""
        self._cancel_recovery()
        task = self._recovery_task
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._recovery_task = None

    async def _recovery_sequence(self) -> None:
        observe_s = self.cfg.phase_recovery_observe
        dwell_s = self.cfg.phase_recovery_dwell
        try:
            # OBSERVE: 405=3 is already written; watch whether the car goes 3p
            # on its own (cooperative cars / plug-in) before we disrupt anything.
            _LOGGER.info("phase recovery: observing up to %ss for real 3-phase", observe_s)
            terminal = await self._observe_phase(observe_s)
            if terminal is not None:
                self._set_recovery(terminal)
                return

            # ESCALATE: the proven fix - a long pause so the car re-negotiates.
            self._recovery_attempted = True   # at most one escalation per 3P request
            self._buffer_commands = True
            _LOGGER.info("phase recovery: still 1-phase, forcing a %ss pause at 0 A", dwell_s)
            await self.coordinator.async_write_owned(R.SET_CURRENT_A, 0)
            if not await self._dwell(dwell_s):
                _LOGGER.info("phase recovery: aborted (car disconnected during pause)")
                self._set_recovery(RECOVERY_ABORTED)
                return

            # No second 405 write: the register is already 3P. Only the pause
            # matters - the car re-reads the phase on its fresh handshake.
            self._set_recovery(RECOVERY_RESUMING, PHASE_RECOVERY_SETTLE_S)
            await asyncio.sleep(PHASE_RECOVERY_SETTLE_S)
            await self._recovery_resume()
            self._set_recovery(RECOVERY_COMPLETE)
            _LOGGER.info("phase recovery: complete, charging resumed")
        except asyncio.CancelledError:
            self._set_recovery(RECOVERY_ABORTED)
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("phase recovery failed: %s", err)
            self._set_recovery(RECOVERY_ABORTED)
        finally:
            if self._recovery_status in (RECOVERY_COMPLETE, RECOVERY_ABORTED):
                self._last_recovery_at = datetime.now(timezone.utc)
                self._last_recovery_result = self._recovery_status
            self._buffer_commands = False
            self._recovery_remaining_s = 0
            self.coordinator.async_update_listeners()
            await self.coordinator.async_request_refresh()

    async def _observe_phase(self, observe_s: int) -> str | None:
        """Return a terminal status if we should stop, or None to escalate."""
        deadline = monotonic() + observe_s
        while True:
            self._set_recovery(RECOVERY_OBSERVING, int(ceil(deadline - monotonic())))
            data = self.coordinator.data
            if data is None or not data.vehicle_connected:
                return RECOVERY_ABORTED
            if not data.charging or self._measured_three_phase(data):
                return RECOVERY_COMPLETE  # went 3p on its own
            if monotonic() >= deadline:
                break
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()
        data = self.coordinator.data
        if data is None or not data.vehicle_connected:
            return RECOVERY_ABORTED
        if not data.charging or self._measured_three_phase(data):
            return RECOVERY_COMPLETE
        if not self._measured_single_phase(data):
            return RECOVERY_COMPLETE  # ambiguous reading -> don't disrupt
        return None  # confirmed still single-phase -> escalate

    async def _dwell(self, dwell_s: int) -> bool:
        """Hold 0 A for the dwell. Return False if the car unplugs meanwhile."""
        deadline = monotonic() + dwell_s
        while monotonic() < deadline:
            self._set_recovery(RECOVERY_DWELLING, int(ceil(deadline - monotonic())))
            await asyncio.sleep(1)
            data = self.coordinator.data
            if data is not None and not data.vehicle_connected:
                return False
        return True

    async def _recovery_resume(self) -> None:
        if self.is_external:
            # Last intent wins. A stop/disable during the pause is respected.
            if self._enabled_intent is False:
                value = 0
            elif self._current_intent is not None:
                value = self._current_intent
            else:
                value = self._ext_resume_current
            value = max(0, min(ABS_MAX_CURRENT_A, int(value)))
            if value > 0:
                self._ext_resume_current = value
            await self.coordinator.async_write_owned(R.SET_CURRENT_A, value)
        else:
            # Internal: let the control loop write the freshly computed setpoint
            # for the new (3-phase) config on the next cycle.
            self._last_setpoint = None

    # -- the per-cycle entry point ------------------------------------------
    async def async_apply(self, data: WallboxData) -> None:
        # Fail closed until this cycle proves every required DLB input healthy.
        # External control does not use this integration's DLB loop.
        self._heartbeat_allowed = not self.cfg.dlb_enabled or self.is_external
        connected = data.vehicle_connected
        just_disconnected = self._was_connected and not connected
        self._was_connected = connected
        # A fresh session re-arms recovery (both modes).
        if just_disconnected:
            self._recovery_attempted = False

        # External control (evcc): stay passive. The heartbeat keeps running in
        # the coordinator, so the wallbox does not drop to its failsafe.
        if self.is_external:
            return
        # A recovery owns the charger while it runs; keep the control loop out.
        if self.recovery_active:
            self._heartbeat_allowed = self.dlb_healthy is True
            return

        # Reset-on-disconnect: revert to the default mode when the car is
        # unplugged, so the next session starts at the configured default.
        if self.cfg.reset_on_disconnect and just_disconnected:
            if self.mode != self.cfg.default_mode:
                self.mode = self.cfg.default_mode

        limits = self.limits
        phases = self._active_phases(data)
        voltage = self._voltage(data)

        surplus_w, surplus_valid = self._surplus(data)
        if surplus_valid and self.mode in SOLAR_MODES:
            surplus_w = self._smooth(surplus_w)
        self.available_surplus_w = surplus_w if surplus_valid else None

        if connected:
            await self._manage_phases(data, voltage, surplus_w, surplus_valid)

        target = ctrl.mode_target_a(
            self.mode,
            manual_a=self.manual_current,
            surplus_w=surplus_w,
            surplus_valid=surplus_valid,
            phases=phases,
            voltage=voltage,
            limits=limits,
        )
        dlb_cap, dlb_error = self._dlb_cap(data)
        if dlb_error is not None:
            await self._apply_dlb_failsafe(data, dlb_error)
            return

        self._set_dlb_health(True if self.cfg.dlb_enabled else None, None)
        self._heartbeat_allowed = True

        # Solar anti-short-cycle may hold its minimum through a cloud transient,
        # but it must run before DLB so grid protection always wins last.
        mode_setpoint = ctrl.finalize_a(
            target,
            dlb_cap=None,
            limits=limits,
            charging_enabled=self.charging_enabled,
            vehicle_connected=connected,
        )
        if self.mode in SOLAR_MODES:
            mode_setpoint = self._anti_short_cycle(mode_setpoint, limits)

        safe_target = ctrl.finalize_a(
            mode_setpoint,
            dlb_cap=dlb_cap,
            limits=limits,
            charging_enabled=self.charging_enabled,
            vehicle_connected=connected,
        )
        setpoint = self._limit_increase(safe_target, data.set_current_a, limits)

        self.computed_setpoint = setpoint
        await self._write_setpoint(setpoint, current_limit=data.set_current_a)

    # -- internals -----------------------------------------------------------
    def _active_phases(self, data: WallboxData) -> int:
        if data.charging and data.phases_in_use > 0:
            return data.phases_in_use
        if data.phase_switch_raw == 0:
            return 1
        if data.phase_switch_raw == 1:
            return 3
        supported = self.coordinator.device.phases_supported
        return 1 if supported == 1 else 3

    def _current_phases(self, data: WallboxData) -> int:
        if data.phase_switch_raw == 0:
            return 1
        if data.phase_switch_raw == 1:
            return 3
        return data.phases_in_use if data.phases_in_use in (1, 3) else 3

    def _charger_supports_3p(self) -> bool:
        # Register 404 (read once at setup). Its exact meaning is not fully
        # verified across firmwares, so we assume 3-phase capability unless it
        # explicitly reports single phase (0). A 405=3 write to a charger that
        # cannot switch is harmless - it simply stays on one phase.
        return self.coordinator.device.phases_supported != 0

    def _target_phase(
        self,
        data: WallboxData,
        voltage: float,
        surplus_w: float,
        surplus_valid: bool,
        current: int,
    ) -> int | None:
        """Desired phase count for the current preference + mode (None = leave as is)."""
        if self.phase_preference == PHASE_1P:
            return 1
        if self.phase_preference == PHASE_3P:
            return 3
        # Auto
        if self.mode in SOLAR_MODES:
            if not surplus_valid:
                return None  # no surplus info -> don't touch the phase
            return ctrl.desired_phases(surplus_w, voltage, self.cfg.solar_min_current, current)
        return 3  # fast / manual auto -> use all phases

    async def _manage_phases(
        self,
        data: WallboxData,
        voltage: float,
        surplus_w: float,
        surplus_valid: bool,
    ) -> None:
        """Drive register 405 towards the desired phase for the current settings.

        The switch is always a single 405 write - the Unite performs its own IEC
        CP interruption. This mirrors evcc's proven approach (one register write,
        no stop/hold sequence). A dwell only applies to solar surplus-based
        auto-switching, to avoid flapping.
        """
        if not self.cfg.phase_switching or not self._charger_supports_3p():
            self._phase_diff_since = None
            return

        current = self._current_phases(data)
        desired = self._target_phase(data, voltage, surplus_w, surplus_valid, current)
        if desired is None or desired == current:
            self._phase_diff_since = None
            return

        now = monotonic()
        # Short post-switch settle so we never write 405 twice in quick
        # succession. The long anti-flap protection for solar is the persist
        # dwell below, NOT this cooldown - otherwise a deliberate (forced) phase
        # change would be silently blocked for minutes.
        if self._last_switch is not None and now - self._last_switch < PHASE_SWITCH_SETTLE_MIN_S:
            return

        # Only solar surplus-based auto-switching needs the anti-flap dwell;
        # a forced phase or a fast/manual choice is applied promptly.
        if self.phase_preference == PHASE_AUTO and self.mode in SOLAR_MODES:
            if self._phase_diff_since is None:
                self._phase_diff_since = now
                return
            if now - self._phase_diff_since < self.cfg.phase_switch_dwell:
                return
        self._phase_diff_since = None

        await self._apply_phase_change(data, current, desired)

    async def _apply_phase_change(self, data: WallboxData, current: int, desired: int) -> None:
        # Single 405 write in every case. Not-yet-charging (status B at plug-in)
        # is undisruptive; switching during charging lets the Unite run its own
        # CP interruption. evcc drives the identical single-write approach.
        if data.charging:
            _LOGGER.info(
                "Switching %s->%s phase(s) during active charging — "
                "wallbox handles the CP interruption internally",
                current,
                desired,
            )
        await self._write_phase(desired)
        self._last_switch = monotonic()
        # If a live 1->3 during charging does not take (car stuck on one phase),
        # the opt-in recovery forces a pause so the car re-negotiates.
        if desired == 3 and current == 1 and self._should_start_recovery(data):
            self._start_recovery()

    async def _write_phase(self, phases: int) -> None:
        value = 1 if phases == 3 else 0
        try:
            await self.coordinator.async_write_owned(R.PHASE_SWITCH, value)
            _LOGGER.info("Switched charging to %s phase(s)", phases)
        except WebastoModbusError as err:
            _LOGGER.warning("Failed to switch to %s phase(s): %s", phases, err)

    def _voltage(self, data: WallboxData) -> float:
        volts = [v for v in (data.voltage_l1_v, data.voltage_l2_v, data.voltage_l3_v) if v]
        measured = sum(volts) / len(volts) if volts else None
        return ctrl.effective_voltage(measured, self.cfg.nominal_voltage)

    def _surplus(self, data: WallboxData) -> tuple[float, bool]:
        model = self.cfg.meter_model
        if model == METER_SURPLUS:
            s = read_power_w(self.hass, self.cfg.surplus_sensor)
            return (s, True) if s is not None else (0.0, False)
        if model == METER_SIGNED_GRID:
            g = read_power_w(self.hass, self.cfg.grid_power_sensor)
            if g is None:
                return 0.0, False
            export = -g if self.cfg.grid_export_negative else g
            return ctrl.available_surplus_w(export, data.active_power_w), True
        if model == METER_DSMR:
            imp = read_power_w(self.hass, self.cfg.import_sensor)
            exp = read_power_w(self.hass, self.cfg.export_sensor)
            if imp is None or exp is None:
                return 0.0, False
            export = exp - imp
            return ctrl.available_surplus_w(export, data.active_power_w), True
        return 0.0, False

    def _dlb_cap(self, data: WallboxData) -> tuple[float | None, str | None]:
        if not self.cfg.dlb_enabled:
            return None, None
        pairs = (
            (self.cfg.dlb_l1, data.current_l1_a),
            (self.cfg.dlb_l2, data.current_l2_a),
            (self.cfg.dlb_l3, data.current_l3_a),
        )
        required = pairs[:1] if self.cfg.dlb_phases == 1 else pairs
        grid_a: list[float] = []
        charger_a: list[float] = []
        plausible_max = max(
            DLB_CURRENT_PLAUSIBLE_FLOOR_A,
            self.cfg.main_fuse_a * 2.0,
        )
        for phase, (sensor_id, charger_current) in enumerate(required, start=1):
            if not sensor_id:
                return None, f"L{phase} grid-current sensor is not configured"
            amps = read_current_a(
                self.hass,
                sensor_id,
                max_age_s=self.cfg.dlb_sensor_max_age,
                not_before=self._started_at,
            )
            if amps is None:
                return None, f"L{phase} grid-current sensor is unavailable or stale"
            if not ctrl.plausible_current(amps, plausible_max):
                _LOGGER.debug("DLB L%s grid current is implausible: %r A", phase, amps)
                return None, f"L{phase} grid current is implausible"
            if not ctrl.plausible_current(charger_current, ABS_MAX_CURRENT_A * 2.0):
                _LOGGER.debug(
                    "DLB L%s charger current is implausible: %r A",
                    phase,
                    charger_current,
                )
                return None, f"L{phase} charger current is implausible"
            grid_a.append(amps)
            charger_a.append(charger_current)
        cap = ctrl.dlb_cap_a(
            self.cfg.main_fuse_a,
            self.cfg.dlb_margin_a,
            grid_a,
            charger_a,
        )
        _LOGGER.debug(
            "DLB calculation: grid=%s A charger=%s A fuse=%s A margin=%s A cap=%.2f A",
            grid_a,
            charger_a,
            self.cfg.main_fuse_a,
            self.cfg.dlb_margin_a,
            cap,
        )
        return cap, None

    def _set_dlb_health(self, healthy: bool | None, reason: str | None) -> None:
        previous = self.dlb_healthy
        previous_reason = self.dlb_failure_reason
        self.dlb_healthy = healthy
        self.dlb_failure_reason = reason
        if healthy is False and (previous is not False or previous_reason != reason):
            _LOGGER.warning(
                "DLB paused: %s; applying %s A failsafe and withholding Alive",
                reason,
                self.cfg.failsafe_current,
            )
        elif healthy is True and previous is False:
            _LOGGER.info(
                "DLB input recovered; normal control and Alive heartbeat resumed"
            )

    async def _apply_dlb_failsafe(self, data: WallboxData, reason: str) -> None:
        self._set_dlb_health(False, reason)
        self._heartbeat_allowed = False
        self._increase_since = None
        setpoint = max(0, min(ABS_MAX_CURRENT_A, self.cfg.failsafe_current))
        self.computed_setpoint = setpoint
        await self._write_setpoint(
            setpoint,
            current_limit=data.set_current_a,
            bypass_quiet=True,
        )

    def _smooth(self, surplus_w: float) -> float:
        now = monotonic()
        self._surplus_window.append((now, surplus_w))
        cutoff = now - SOLAR_SMOOTHING_S
        while self._surplus_window and self._surplus_window[0][0] < cutoff:
            self._surplus_window.popleft()
        values = [w for _, w in self._surplus_window]
        return sum(values) / len(values)

    def _anti_short_cycle(self, setpoint: int, limits: ctrl.Limits) -> int:
        now = monotonic()
        if setpoint > 0:
            if self._charge_started is None:
                self._charge_started = now
            return setpoint
        # A pause was requested. Hold the minimum until we have charged long
        # enough, to avoid rapid relay cycling on passing clouds.
        if (
            self._charge_started is not None
            and now - self._charge_started < SOLAR_MIN_CHARGE_DURATION_S
            and self.charging_enabled
            and self._was_connected
        ):
            return limits.min_current
        self._charge_started = None
        return 0

    def _limit_increase(
        self,
        target: int,
        current_limit: int | None,
        limits: ctrl.Limits,
    ) -> int:
        """Apply immediate reductions and delayed, stepped increases."""
        current = current_limit
        if current is None:
            current = self._last_setpoint
        if current is None:
            current = max(0, min(ABS_MAX_CURRENT_A, self.cfg.failsafe_current))

        if target <= current:
            self._increase_since = None
            return target

        now = monotonic()
        if self._increase_since is None:
            self._increase_since = now
            if self.cfg.increase_delay > 0:
                return current
        if now - self._increase_since < self.cfg.increase_delay:
            return current

        self._increase_since = now
        if current < limits.min_current:
            return min(target, limits.min_current)
        return min(target, current + self.cfg.increase_step)

    async def _write_setpoint(
        self,
        setpoint: int,
        *,
        current_limit: int | None = None,
        bypass_quiet: bool = False,
    ) -> None:
        # Quiet period right after a phase switch: leave the current setpoint
        # alone so the wallbox can finish its internal CP interruption. Safety
        # reductions and failsafe writes always bypass this delay.
        previous = current_limit if current_limit is not None else self._last_setpoint
        reducing = previous is not None and setpoint < previous
        if (
            not bypass_quiet
            and not reducing
            and self._last_switch is not None
            and monotonic() - self._last_switch < PHASE_SWITCH_QUIET_S
        ):
            _LOGGER.debug("Holding charge-current write during post-phase-switch quiet period")
            return
        if current_limit is not None and setpoint == current_limit:
            self._last_setpoint = setpoint
            return
        if current_limit is None and setpoint == self._last_setpoint:
            return
        await self.coordinator.async_write_owned(R.SET_CURRENT_A, setpoint)
        self._last_setpoint = setpoint
        _LOGGER.debug("Wrote charge current setpoint: %s A", setpoint)
