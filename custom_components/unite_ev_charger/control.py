"""Pure charging-control logic for the Unite EV Charger.

Everything here is deliberately free of Home Assistant and hardware: given
numbers in, it returns the target charge current. That makes the decision logic
fully unit-testable without a charger - exactly where the old integration was
guesswork. State (smoothing, dwell timers, last setpoint) lives in the
HA-aware controller; this module only does the math.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import (
    MODE_FAST,
    MODE_MANUAL,
    MODE_MIN_SOLAR,
    MODE_SOLAR,
    NOMINAL_VOLTAGE,
    PHASE_MEASURE_OFF_A,
    PHASE_MEASURE_ON_A,
    PHASE_SWITCH_HYSTERESIS,
    PLAUSIBLE_VOLTAGE_MAX,
    PLAUSIBLE_VOLTAGE_MIN,
)


def is_phase_mismatch(
    charging: bool,
    phase_switch_raw: int | None,
    l1: float,
    l2: float,
    l3: float,
) -> bool:
    """True when the charger is set to 3-phase (405=1) but the car draws only 1.

    Only asserted with a confident single-phase reading while charging, so a
    ramping car does not raise a false alarm.
    """
    if not charging:
        return False
    configured_3p = phase_switch_raw == 1
    measured_1p = l1 >= PHASE_MEASURE_ON_A and l2 < PHASE_MEASURE_OFF_A and l3 < PHASE_MEASURE_OFF_A
    return configured_3p and measured_1p


def derive_charger_state(
    *,
    connection_ok: bool,
    restarting: bool,
    faulted: bool,
    vehicle_connected: bool,
    charging: bool,
    phase_mismatch: bool,
    recovery_active: bool,
) -> str:
    """Interpret the wallbox state. Priority order - first match wins."""
    if restarting:
        return "restarting"        # reboot is known -> takes priority over 'offline'
    if not connection_ok:
        return "disconnected"      # covers offline / failsafe
    if faulted:
        return "fault"
    if recovery_active:
        return "recovery"
    if vehicle_connected and charging:
        return "phase_mismatch" if phase_mismatch else "charging"
    if vehicle_connected:
        return "connected"         # plugged in, not charging (status B)
    return "idle"                  # no car (status A)


@dataclass(frozen=True)
class Limits:
    """Current bounds for the wallbox."""

    min_current: int
    max_current: int
    cable_max: int | None = None

    def effective_max(self) -> int:
        m = self.max_current
        if self.cable_max:
            m = min(m, self.cable_max)
        return max(m, self.min_current)


def effective_poll_interval(poll_interval: int, failsafe_timeout: int) -> int:
    """Clamp the poll interval so the Alive heartbeat is refreshed fast enough.

    The Unite drops the Modbus socket (and resets register 405 to its default)
    if it does not see an Alive refresh within the failsafe timeout; the spec
    recommends refreshing every ``timeout/2`` (floor 3 s). We write the heartbeat
    once per poll, so the poll must never be slower than that - otherwise a slow
    poll causes spurious failsafe + phase resets. The user's configured value is
    kept; this only makes the effective tick faster when needed.
    """
    return min(poll_interval, max(3, failsafe_timeout // 2))


def effective_voltage(measured: float | None, nominal: float = NOMINAL_VOLTAGE) -> float:
    """Use the charger's measured voltage when plausible, else a nominal value."""
    if measured is not None and PLAUSIBLE_VOLTAGE_MIN <= measured <= PLAUSIBLE_VOLTAGE_MAX:
        return float(measured)
    return float(nominal)


def available_surplus_w(export_w: float, charger_power_w: float) -> float:
    """PV available to the car = grid export + what the car already draws.

    Adding the charger's own power back makes the surplus a stable reference
    that does not chase itself as the car ramps up/down.
    """
    return export_w + charger_power_w


def current_from_power(power_w: float, phases: int, voltage: float) -> float:
    if phases <= 0 or voltage <= 0:
        return 0.0
    return power_w / (phases * voltage)


def solar_target_a(
    mode: str,
    surplus_w: float,
    surplus_valid: bool,
    phases: int,
    voltage: float,
    limits: Limits,
) -> float:
    """Target current for the two solar modes.

    * solar (pure): only charge when surplus supports at least the minimum,
      otherwise pause. On invalid/stale input: pause.
    * min_solar (min-follow): at least the minimum, follow surplus above it.
      On invalid/stale input: keep charging at the minimum.
    """
    if not surplus_valid:
        return float(limits.min_current) if mode == MODE_MIN_SOLAR else 0.0

    raw = current_from_power(surplus_w, phases, voltage)
    if mode == MODE_MIN_SOLAR:
        return float(min(max(limits.min_current, raw), limits.effective_max()))
    # pure solar
    if raw < limits.min_current:
        return 0.0
    return float(min(raw, limits.effective_max()))


def mode_target_a(
    mode: str,
    *,
    manual_a: int,
    surplus_w: float,
    surplus_valid: bool,
    phases: int,
    voltage: float,
    limits: Limits,
) -> float:
    """The current a given mode wants, before DLB capping and final clamps."""
    if mode == MODE_FAST:
        return float(limits.effective_max())
    if mode == MODE_MANUAL:
        return float(max(limits.min_current, min(manual_a, limits.effective_max())))
    if mode in (MODE_SOLAR, MODE_MIN_SOLAR):
        return solar_target_a(mode, surplus_w, surplus_valid, phases, voltage, limits)
    return 0.0


def dlb_cap_a(
    fuse: float,
    margin: float,
    grid_a: list[float],
    charger_a: list[float],
) -> float:
    """Per-phase DLB ceiling.

    On each phase the room for the car is ``fuse - margin - (grid - charger)``;
    subtracting the charger's own draw isolates the rest of the house. The cap
    is the lowest room across the phases the car charges on.
    """
    caps = [fuse - margin - (g - c) for g, c in zip(grid_a, charger_a)]
    return min(caps) if caps else float("inf")


def desired_phases(
    surplus_w: float,
    voltage: float,
    min_current: int,
    current_phases: int,
) -> int:
    """Pick 1 or 3 phases for solar charging, with hysteresis.

    Upshift to 3 phases only when the surplus can sustain the 3-phase minimum
    (otherwise 3p cannot even start); drop back to 1 phase when the surplus
    falls clearly below that, so it does not toggle on the boundary. The dwell
    timer in the controller is what actually delays acting on this.
    """
    three_phase_min_w = min_current * 3 * voltage
    upshift = three_phase_min_w
    downshift = three_phase_min_w * PHASE_SWITCH_HYSTERESIS
    if current_phases <= 1:
        return 3 if surplus_w >= upshift else 1
    return 1 if surplus_w < downshift else 3


def finalize_a(
    target_a: float,
    *,
    dlb_cap: float | None,
    limits: Limits,
    charging_enabled: bool,
    vehicle_connected: bool,
) -> int:
    """Combine mode target + DLB cap + limits into a whole-amp setpoint.

    Returns 0 (pause) when charging is off, no car is connected, or there is no
    room to sustain the minimum current.
    """
    if not charging_enabled or not vehicle_connected:
        return 0
    target = target_a
    if dlb_cap is not None:
        target = min(target, dlb_cap)
    target = min(target, limits.effective_max())
    whole = int(target)  # floor to whole amps
    if whole < limits.min_current:
        return 0
    return whole
