"""Unit tests for the pure charging-control logic."""
from __future__ import annotations

import math

from uec import control as C
from uec.const import MODE_FAST, MODE_MANUAL, MODE_MIN_SOLAR, MODE_SOLAR

LIMITS = C.Limits(min_current=6, max_current=16, cable_max=32)


# --- state inspector + phase mismatch ---------------------------------------
def test_is_phase_mismatch():
    # set to 3-phase (405=1) but only L1 drawing -> mismatch
    assert C.is_phase_mismatch(True, 1, 15.0, 0.0, 0.4) is True
    # genuinely 3-phase -> no mismatch
    assert C.is_phase_mismatch(True, 1, 15.0, 15.0, 15.0) is False
    # single-phase configured -> no mismatch
    assert C.is_phase_mismatch(True, 0, 15.0, 0.0, 0.0) is False
    # not charging -> never a mismatch
    assert C.is_phase_mismatch(False, 1, 0.0, 0.0, 0.0) is False
    # unknown register -> no false alarm
    assert C.is_phase_mismatch(True, None, 15.0, 0.0, 0.0) is False


def _state(**over):
    base = dict(
        connection_ok=True, restarting=False, faulted=False,
        vehicle_connected=False, charging=False, phase_mismatch=False,
        recovery_active=False,
    )
    base.update(over)
    return C.derive_charger_state(**base)


def test_derive_charger_state_priority_order():
    # restarting wins over everything, even a failed connection
    assert _state(restarting=True, connection_ok=False, charging=True) == "restarting"
    # disconnected wins over fault/charging
    assert _state(connection_ok=False, faulted=True, charging=True) == "disconnected"
    # fault wins over recovery/charging
    assert _state(faulted=True, recovery_active=True, vehicle_connected=True, charging=True) == "fault"
    # recovery wins over charging
    assert _state(recovery_active=True, vehicle_connected=True, charging=True) == "recovery"
    # charging vs phase mismatch
    assert _state(vehicle_connected=True, charging=True) == "charging"
    assert _state(vehicle_connected=True, charging=True, phase_mismatch=True) == "phase_mismatch"
    # plugged in but not charging
    assert _state(vehicle_connected=True, charging=False) == "connected"
    # no car
    assert _state() == "idle"


# --- helpers ----------------------------------------------------------------
def test_effective_poll_interval_clamps_to_half_failsafe_timeout():
    # (poll, timeout) -> effective, per the Vestel Alive/failsafe spec.
    assert C.effective_poll_interval(10, 30) == 10   # already fast enough
    assert C.effective_poll_interval(30, 30) == 15   # clamped to timeout/2
    assert C.effective_poll_interval(60, 20) == 10
    assert C.effective_poll_interval(10, 10) == 5
    assert C.effective_poll_interval(10, 5) == 3     # floor at 3 s
    assert C.effective_poll_interval(10, 4) == 3     # timeout/2 = 2 -> floored to 3


def test_effective_voltage_uses_measured_when_plausible():
    assert C.effective_voltage(230) == 230
    assert C.effective_voltage(0) == 230  # implausible -> nominal
    assert C.effective_voltage(None) == 230
    assert C.effective_voltage(400, nominal=230) == 230


def test_effective_max_respects_cable_and_min():
    assert C.Limits(6, 16, 32).effective_max() == 16
    assert C.Limits(6, 16, 10).effective_max() == 10  # cable lower
    assert C.Limits(6, 4, None).effective_max() == 6  # never below min


def test_available_surplus_adds_charger_power_back():
    # Exporting 1380 W while the car already pulls 3680 W -> 5060 W available.
    assert C.available_surplus_w(1380, 3680) == 5060


def test_current_from_power():
    assert C.current_from_power(3680, 1, 230) == 16.0
    assert math.isclose(C.current_from_power(11040, 3, 230), 16.0)
    assert C.current_from_power(1000, 0, 230) == 0.0


# --- solar modes ------------------------------------------------------------
def test_solar_pure_pauses_below_minimum():
    # 1.0 kW on 1 phase @230V ~ 4.3 A < 6 A min -> pause
    assert C.solar_target_a(MODE_SOLAR, 1000, True, 1, 230, LIMITS) == 0.0


def test_solar_pure_follows_surplus_and_clamps():
    # 2.3 kW -> 10 A
    assert C.solar_target_a(MODE_SOLAR, 2300, True, 1, 230, LIMITS) == 10.0
    # huge surplus clamps to effective max (16)
    assert C.solar_target_a(MODE_SOLAR, 100000, True, 1, 230, LIMITS) == 16.0


def test_min_solar_follows_but_never_below_min():
    # low surplus -> still minimum (min-volg)
    assert C.solar_target_a(MODE_MIN_SOLAR, 500, True, 1, 230, LIMITS) == 6.0
    # enough surplus -> follow it
    assert C.solar_target_a(MODE_MIN_SOLAR, 2300, True, 1, 230, LIMITS) == 10.0


def test_stale_sensor_behaviour_per_mode():
    # invalid/stale input: pure solar pauses, min_solar holds minimum
    assert C.solar_target_a(MODE_SOLAR, 0, False, 1, 230, LIMITS) == 0.0
    assert C.solar_target_a(MODE_MIN_SOLAR, 0, False, 1, 230, LIMITS) == 6.0


def test_mode_target_fast_and_manual():
    assert C.mode_target_a(MODE_FAST, manual_a=8, surplus_w=0, surplus_valid=False,
                           phases=1, voltage=230, limits=LIMITS) == 16.0
    assert C.mode_target_a(MODE_MANUAL, manual_a=10, surplus_w=0, surplus_valid=False,
                           phases=1, voltage=230, limits=LIMITS) == 10.0
    # manual is clamped into [min, effective_max]
    assert C.mode_target_a(MODE_MANUAL, manual_a=99, surplus_w=0, surplus_valid=False,
                           phases=1, voltage=230, limits=LIMITS) == 16.0


# --- DLB --------------------------------------------------------------------
def test_dlb_cap_single_phase_example():
    # fuse 25, margin 0, grid 18 A (10 house + 8 car), car 8 A -> room 15 A
    assert C.dlb_cap_a(25, 0, [18.0], [8.0]) == 15.0


def test_dlb_cap_takes_lowest_phase_and_margin():
    # L1 room = 25-2-(20-5)=8 ; L2 room = 25-2-(12-5)=16 -> min = 8
    assert C.dlb_cap_a(25, 2, [20.0, 12.0], [5.0, 5.0]) == 8.0


def test_failsafe_current_never_uses_invalid_one_to_five_amp_range():
    assert C.normalize_failsafe_current(0) == 0
    assert C.normalize_failsafe_current(3) == 6
    assert C.normalize_failsafe_current(6) == 6
    assert C.normalize_failsafe_current(99) == 32


# --- finalize ---------------------------------------------------------------
def test_finalize_pauses_when_disabled_or_no_vehicle():
    assert C.finalize_a(16, dlb_cap=None, limits=LIMITS,
                        charging_enabled=False, vehicle_connected=True) == 0
    assert C.finalize_a(16, dlb_cap=None, limits=LIMITS,
                        charging_enabled=True, vehicle_connected=False) == 0


def test_finalize_applies_dlb_cap_and_floors():
    # target 16, DLB cap 10.7 -> floor 10
    assert C.finalize_a(16, dlb_cap=10.7, limits=LIMITS,
                        charging_enabled=True, vehicle_connected=True) == 10


def test_finalize_pauses_when_below_minimum():
    # DLB only allows 4 A, below 6 A min -> pause
    assert C.finalize_a(16, dlb_cap=4, limits=LIMITS,
                        charging_enabled=True, vehicle_connected=True) == 0


# --- phase switching --------------------------------------------------------
def test_phase_upshift_only_when_3p_minimum_reachable():
    # 3p minimum @6A/230V = 4140 W
    # on 1 phase, just below -> stay 1p; at/above -> go 3p
    assert C.desired_phases(4000, 230, 6, current_phases=1) == 1
    assert C.desired_phases(4140, 230, 6, current_phases=1) == 3


def test_phase_downshift_has_hysteresis():
    # on 3 phases: hold until clearly below 0.9 * 4140 = 3726 W
    assert C.desired_phases(3800, 230, 6, current_phases=3) == 3  # still holds
    assert C.desired_phases(3700, 230, 6, current_phases=3) == 1  # drops


def test_phase_no_flapping_in_hysteresis_band():
    # In the band [3726, 4140) the phase count stays whatever it currently is.
    assert C.desired_phases(3900, 230, 6, current_phases=1) == 1
    assert C.desired_phases(3900, 230, 6, current_phases=3) == 3
