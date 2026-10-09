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
    CONF_EXPORT_SENSOR,
    CONF_GRID_EXPORT_NEGATIVE,
    CONF_GRID_POWER_SENSOR,
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
    DEFAULT_MAIN_FUSE_A,
    DEFAULT_MAX_CURRENT_A,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_MODE,
    DEFAULT_PHASE_RECOVERY_DWELL_S,
    DEFAULT_PHASE_RECOVERY_ENABLED,
    DEFAULT_PHASE_RECOVERY_OBSERVE_S,
    DEFAULT_PHASE_SWITCH_DWELL_S,
    DLB_PLAUSIBLE_CURRENT_FLOOR_A,
    DLB_SENSOR_MAX_AGE_S,
    DEFAULT_PHASE_PREFERENCE,
    DEFAULT_PHASE_SWITCHING,
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
from .phase import PhaseRecoveryMixin

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
    dlb_l1: str | None
    dlb_l2: str | None
    dlb_l3: str | None
    phase_switching: bool
    phase_switch_dwell: int
    phase_recovery_enabled: bool
    phase_recovery_observe: int
    phase_recovery_dwell: int

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
        )


class ChargeControl(PhaseRecoveryMixin):
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

        # Internal state.
        self._surplus_window: deque[tuple[float, float]] = deque()
        self._last_setpoint: int | None = None
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
        self._downshift_attempted: bool = False  # latch: one escalation per 1P request
        self._recovery_direction: str | None = None  # "up"/"down" of last attempt
        self._guard_mismatches: int = 0  # trede 1: consecutive wish-vs-405 polls
        self._buffer_commands: bool = False       # hold evcc's writes during the pause
        self._dlb_block_reason: str | None = None
        self._last_recovery_at: datetime | None = None
        self._last_recovery_result: str | None = None
        # evcc-facing intent (what evcc last asked). The entities report this so
        # evcc stays "in sync" even while the hardware is briefly forced to 0 A.
        self._requested_phase: str | None = None
        self._current_intent: int | None = None
        self._enabled_intent: bool | None = None

    def _record(self, kind: str, detail: str) -> None:
        """Append a diagnostics-only event, if the coordinator has a log.

        Tolerates the plain FakeCoordinator used in unit tests (no event log),
        so recording is always safe and never changes control behaviour.
        """
        recorder = getattr(self.coordinator, "record_event", None)
        if recorder is not None:
            recorder(kind, detail)

    # -- helpers used by entities -------------------------------------------
    @property
    def is_external(self) -> bool:
        return self.cfg.control_mode == CONTROL_EXTERNAL

    @property
    def recovery_active(self) -> bool:
        return self._recovery_task is not None and not self._recovery_task.done()

    @property
    def recovery_status(self) -> str:
        return self._recovery_status

    @property
    def recovery_direction(self) -> str | None:
        """"up" (1->3), "down" (3->1) or None when no attempt ran yet."""
        return self._recovery_direction

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

    # -- external (evcc) direct writes --------------------------------------
    # Faithful passthrough: evcc owns phase/current/enable, each command is one
    # register write - like evcc's own Vestel driver. The only exception is the
    # opt-in 1->3 recovery: while its forced pause runs, evcc's current/enable
    # commands are buffered (a 0 A / stop still passes straight through) and the
    # entities report evcc's intent so evcc stays in sync.
    async def async_external_set_current(self, amps: float) -> None:
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
                await self.coordinator.client.write_register(R.SET_CURRENT_A, 0)
            return
        await self.coordinator.client.write_register(R.SET_CURRENT_A, value)
        await self.coordinator.async_request_refresh()

    async def async_external_set_enabled(self, enabled: bool) -> None:
        await self.async_external_set_current(self._ext_resume_current if enabled else 0)

    async def async_external_set_phase(self, phases: int) -> None:
        if phases != 3:
            self._requested_phase = "1"
            self._recovery_attempted = False   # a 1P request re-arms recovery
            self._cancel_recovery()
            self.coordinator.async_update_listeners()
            await self._write_phase(1, who="evcc")
            await self.coordinator.async_request_refresh()
            if self._should_start_downshift(self.coordinator.data):
                self._start_recovery("down")
            return
        # phases == 3: live write first, then adaptive recovery if it didn't take.
        self._requested_phase = "3"
        self._downshift_attempted = False  # a 3P request re-arms downshift
        self.coordinator.async_update_listeners()
        await self._write_phase(3, who="evcc")
        await self.coordinator.async_request_refresh()
        if self._should_start_recovery(self.coordinator.data):
            self._start_recovery("up")


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
            # "Off" must land on the wire immediately, not one cycle later: at a
            # new session the wallbox has just applied its own hardware minimum,
            # so with charging switched off write 0 A now (the loop then confirms
            # the same value this same cycle).
            if not self.charging_enabled:
                await self.coordinator.client.write_register(R.SET_CURRENT_A, 0)

    async def _reassert_phase_on_reconnect(
        self, phase_raw: int | None, *, context: str = "reconnect"
    ) -> None:
        """Restore the desired phase: the wallbox reset register 405 to its 404
        default on the disconnect (per spec).

        Internal mode's control loop re-evaluates the phase this same cycle, so
        only the external (evcc) path needs an explicit re-assert - and only when
        the reset default actually differs from evcc's request, to avoid a
        needless CP interruption. ``context`` only selects the event kind recorded
        on an actual write ("reconnect" vs "session").
        """
        if not self.is_external or self._requested_phase not in (PHASE_1P, PHASE_3P):
            return
        desired_raw = 1 if self._requested_phase == PHASE_3P else 0
        if phase_raw == desired_raw:
            return  # reset default already matches -> no write, no CP blip
        await self.coordinator.client.write_register(R.PHASE_SWITCH, desired_raw)
        if context == "session":
            self._record(
                "phase_reassert_session",
                f"requested={self._requested_phase}P measured={phase_raw} "
                f"written={desired_raw}",
            )
        else:
            self._record("405_write", f"reconnect reassert reg={desired_raw}")

    async def _guard_phase_setting(self, data: WallboxData) -> None:
        """Trede 1: keep register 405 converged with evcc's wish, mid-session.

        Only ever rewrites evcc's own wish (never invents one), only after 3
        consecutive polls of disagreement (ramping never trips it), and never
        while a recovery owns the charger. Worst case of a wrong call is one
        redundant identical write.
        """
        if self.recovery_active:
            self._guard_mismatches = 0
            return
        wish = self._requested_phase
        if wish not in (PHASE_1P, PHASE_3P):
            self._guard_mismatches = 0
            return
        if not data.vehicle_connected:
            self._guard_mismatches = 0
            return
        desired_raw = 1 if wish == PHASE_3P else 0
        if data.phase_switch_raw is None or data.phase_switch_raw == desired_raw:
            self._guard_mismatches = 0
            return
        self._guard_mismatches += 1
        if self._guard_mismatches < 3:
            return
        self._guard_mismatches = 0
        await self._write_phase(3 if wish == PHASE_3P else 1, who="guard")

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


    # -- the per-cycle entry point ------------------------------------------
    async def async_apply(self, data: WallboxData) -> None:
        connected = data.vehicle_connected
        just_disconnected = self._was_connected and not connected
        just_connected = connected and not self._was_connected
        self._was_connected = connected
        # A fresh session re-arms recovery (both modes).
        if just_disconnected:
            self._recovery_attempted = False
            self._downshift_attempted = False
            self._guard_mismatches = 0
            reset = getattr(self.coordinator, "reset_fix_failures", None)
            if reset is not None:
                reset()
        # Plugging in starts a new session and the wallbox sets its own charge
        # current for it (its hardware minimum). Our cached "last written"
        # setpoint is therefore stale: without this, a target that happens to be
        # unchanged - notably 0 A while charging is switched off - would be
        # skipped as "already applied" and the car would charge at the wallbox's
        # minimum with the Charging switch still off.
        if just_connected:
            await self._reassert_current_on_reconnect()
            # External (evcc) only: re-assert the requested phase if the freshly
            # read 405 drifted from evcc's request. Internal mode re-evaluates the
            # phase later this same cycle, so it is left untouched.
            if (
                self.is_external
                and not self.recovery_active
                and data.phase_switch_raw is not None
            ):
                await self._reassert_phase_on_reconnect(
                    data.phase_switch_raw, context="session"
                )

        # External control (evcc): stay passive. The heartbeat keeps running in
        # the coordinator, so the wallbox does not drop to its failsafe.
        if self.is_external:
            await self._guard_phase_setting(data)
            return
        # A recovery owns the charger while it runs; keep the control loop out.
        if self.recovery_active:
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
        dlb_cap = self._dlb_cap(data)
        setpoint = ctrl.finalize_a(
            target,
            dlb_cap=dlb_cap,
            limits=limits,
            charging_enabled=self.charging_enabled,
            vehicle_connected=connected,
        )
        if self.mode in SOLAR_MODES:
            setpoint = self._anti_short_cycle(setpoint, limits)

        self.computed_setpoint = setpoint
        await self._write_setpoint(setpoint)

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
            self._start_recovery("up")
        # Mirror for 3->1: a car stuck on three phases gets the same pause.
        if desired == 1 and current == 3 and self._should_start_downshift(data):
            self._start_recovery("down")
        if desired == 3:
            self._downshift_attempted = False  # a new 3P cycle re-arms downshift

    async def _write_phase(self, phases: int, *, who: str = "controller") -> None:
        value = 1 if phases == 3 else 0
        try:
            await self.coordinator.client.write_register(R.PHASE_SWITCH, value)
            self._record("405_write", f"{who} {phases}P (reg={value})")
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

    def _dlb_cap(self, data: WallboxData) -> float | None:
        """The DLB ceiling, or 0 A when the inputs cannot be trusted.

        DLB's only job is to keep every phase under the main fuse, so it must
        fail CLOSED: missing, stale or implausible grid data is not evidence of
        available headroom. Every sensor the user configured must produce a
        fresh, plausible reading - a partial picture could hand out room on a
        phase we cannot see.
        """
        if not self.cfg.dlb_enabled:
            return None
        pairs = (
            (self.cfg.dlb_l1, data.current_l1_a, "L1"),
            (self.cfg.dlb_l2, data.current_l2_a, "L2"),
            (self.cfg.dlb_l3, data.current_l3_a, "L3"),
        )
        configured = [(sid, amps, name) for sid, amps, name in pairs if sid]
        if not configured:
            return self._dlb_blocked("no grid-current sensor is configured")

        plausible_max = max(DLB_PLAUSIBLE_CURRENT_FLOOR_A, self.cfg.main_fuse_a * 2.0)
        grid_a: list[float] = []
        charger_a: list[float] = []
        for sensor_id, charger_current, name in configured:
            amps = read_current_a(self.hass, sensor_id, max_age_s=DLB_SENSOR_MAX_AGE_S)
            if amps is None:
                return self._dlb_blocked(f"{name} grid-current sensor is unavailable or stale")
            if not ctrl.plausible_current(amps, plausible_max):
                return self._dlb_blocked(f"{name} grid current is implausible ({amps} A)")
            grid_a.append(amps)
            charger_a.append(charger_current)

        self._dlb_clear()
        return ctrl.dlb_cap_a(self.cfg.main_fuse_a, self.cfg.dlb_margin_a, grid_a, charger_a)

    def _dlb_blocked(self, reason: str) -> float:
        """Pause charging (0 A) and log the reason once per transition."""
        if self._dlb_block_reason != reason:
            _LOGGER.warning("Load balancing paused charging: %s", reason)
            self._dlb_block_reason = reason
        return 0.0

    def _dlb_clear(self) -> None:
        if self._dlb_block_reason is not None:
            _LOGGER.info("Load balancing inputs healthy again; resuming")
            self._dlb_block_reason = None

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

    def invalidate_setpoint_cache(self) -> None:
        """Forget what we last wrote to register 5004.

        Call this whenever something outside the control loop may have changed
        the charger's current limit (a web-UI action, a reconnect), so the next
        cycle rewrites it instead of assuming the charger still has our value.
        """
        self._last_setpoint = None

    def set_charging_enabled(self, enabled: bool) -> None:
        """Apply the internal charging switch.

        Turning ON only records the intent; the control loop applies the target
        on the next cycle as before. Turning OFF also forgets the last written
        setpoint, so the loop's 0 A is written for real instead of being skipped
        as an "already applied" silent repetition - an explicit off must land on
        the wire, never be optimised away.
        """
        self.charging_enabled = enabled
        if not enabled:
            self.invalidate_setpoint_cache()

    async def _write_setpoint(self, setpoint: int) -> None:
        # Quiet period right after a phase switch: leave the current setpoint
        # alone so the wallbox can finish its internal CP interruption. The
        # heartbeat (written by the coordinator) keeps going regardless.
        if self._last_switch is not None and monotonic() - self._last_switch < PHASE_SWITCH_QUIET_S:
            _LOGGER.debug("Holding charge-current write during post-phase-switch quiet period")
            return
        if setpoint == self._last_setpoint:
            return
        try:
            await self.coordinator.client.write_register(R.SET_CURRENT_A, setpoint)
            self._last_setpoint = setpoint
            _LOGGER.debug("Wrote charge current setpoint: %s A", setpoint)
        except WebastoModbusError as err:
            _LOGGER.warning("Failed to write charge current setpoint %s A: %s", setpoint, err)
