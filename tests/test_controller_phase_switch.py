"""Controller tests for the guarded phase-switch + post-switch quiet period.

The controller only imports Home Assistant for type hints (stubbed in conftest),
so we can drive its methods directly with a fake Modbus client - no HA, no
hardware. Time is controlled by setting the internal timers directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

from uec import registers as R
from uec.controller import ChargeControl
from uec.models import WallboxData

OPTIONS = {
    "default_mode": "solar",
    "min_current": 6,
    "max_current": 16,
    "solar_min_current": 6,
    "phase_switching": True,
    "phase_switch_dwell": 300,
    "meter_model": "surplus",
}


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    async def write_register(self, reg, value) -> None:
        self.writes.append((reg.name, value))


class FakeCoordinator:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.device = SimpleNamespace(max_current_a=16, min_current_a=6, phases_supported=3)
        self.data = None

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


def _make_control():
    client = FakeClient()
    ctl = ChargeControl(object(), SimpleNamespace(options=OPTIONS), FakeCoordinator(client))
    ctl.mode = "solar"
    return ctl, client


def _data(phase_switch_raw: int = 0, state: int = 2) -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = state  # 2 = charging
    d.cable_state_raw = 2             # vehicle connected
    d.phase_switch_raw = phase_switch_raw
    d.current_l1_a = 16.0
    return d


def test_writes_405_only_after_dwell():
    ctl, client = _make_control()
    # Not charging (status B); a phase switch is a single direct 405 write.
    data = _data(phase_switch_raw=0, state=1)

    async def run():
        # 5000 W >= 3p minimum (6*3*230=4140) -> wants 3 phases
        await ctl._manage_phases(data, 230, 5000, True)
        first = list(client.writes)
        # pretend the desired phase has persisted longer than the dwell
        ctl._phase_diff_since = monotonic() - 400
        await ctl._manage_phases(data, 230, 5000, True)
        return first, client.writes

    first, after = asyncio.run(run())
    assert first == []                       # dwell not elapsed -> no switch yet
    assert ("phase_switch", 1) in after      # switched to 3 phases (405 = 1)
    assert ctl._last_switch is not None
    assert ctl._phase_diff_since is None


def test_no_switch_when_already_correct_phase():
    ctl, client = _make_control()
    data = _data(phase_switch_raw=1)  # already 3 phases

    async def run():
        await ctl._manage_phases(data, 230, 5000, True)
        return client.writes

    assert asyncio.run(run()) == []


def test_cooldown_blocks_new_switch():
    ctl, client = _make_control()
    data = _data(phase_switch_raw=0)  # 1p, wants 3p

    async def run():
        ctl._last_switch = monotonic()             # just switched
        ctl._phase_diff_since = monotonic() - 400  # dwell already elapsed
        await ctl._manage_phases(data, 230, 5000, True)
        return client.writes

    assert asyncio.run(run()) == []  # settling/cooldown blocks it


def test_target_phase_forced_and_auto():
    ctl, _ = _make_control()
    data = _data(phase_switch_raw=0)
    ctl.phase_preference = "1"
    assert ctl._target_phase(data, 230, 5000, True, 1) == 1
    ctl.phase_preference = "3"
    assert ctl._target_phase(data, 230, 5000, True, 1) == 3
    # auto + fast/manual -> all phases
    ctl.phase_preference = "auto"
    ctl.mode = "fast"
    assert ctl._target_phase(data, 230, 0, False, 1) == 3
    ctl.mode = "manual"
    assert ctl._target_phase(data, 230, 0, False, 1) == 3
    # auto + solar without surplus info -> leave as is
    ctl.mode = "solar"
    assert ctl._target_phase(data, 230, 0, False, 1) is None


def test_fast_default_goes_3p_at_plugin_directly():
    ctl, client = _make_control()
    ctl.mode = "fast"  # phase_preference defaults to auto
    data = _data(phase_switch_raw=0, state=1)  # connected (B), not charging, 1-phase

    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    assert ("phase_switch", 1) in client.writes  # direct write to 3-phase


def test_force_one_phase_downshifts_directly_while_charging():
    ctl, client = _make_control()
    ctl.mode = "fast"
    ctl.phase_preference = "1"
    data = _data(phase_switch_raw=1, state=2)  # charging on 3 phases
    data.current_l2_a = 10.0
    data.current_l3_a = 10.0

    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    assert ("phase_switch", 0) in client.writes


def test_force_3p_while_charging_1p_writes_405_directly():
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.phase_preference = "3"
    data = _data(phase_switch_raw=0, state=2)  # charging on a single phase

    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    # A single 405=1 write, during charging - the Unite runs its own CP
    # interruption (evcc's proven approach); no stop/hold recovery sequence.
    assert client.writes == [("phase_switch", 1)]


def test_forced_phase_change_not_blocked_by_long_cooldown():
    ctl, client = _make_control()
    ctl.mode = "fast"
    ctl.phase_preference = "3"
    data = _data(phase_switch_raw=0, state=1)  # 1-phase, not charging
    # A switch happened 20 s ago: past the short settle (15 s) but well within
    # the old 300 s dwell. A deliberate change must still go through.
    ctl._last_switch = monotonic() - 20
    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    assert ("phase_switch", 1) in client.writes


def test_one_phase_charger_never_switches():
    ctl, client = _make_control()
    ctl.coordinator.device.phases_supported = 0  # 404 == 0 -> single phase
    ctl.mode = "fast"
    data = _data(phase_switch_raw=0, state=1)

    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    assert client.writes == []


def test_phase_switching_disabled_never_touches_405():
    ctl, client = _make_control()
    ctl.cfg = replace(ctl.cfg, phase_switching=False)  # master opt-in off
    ctl.mode = "fast"
    ctl.phase_preference = "3"
    data = _data(phase_switch_raw=0, state=1)  # would otherwise want 3p

    asyncio.run(ctl._manage_phases(data, 230, 0, False))
    assert client.writes == []


def test_setpoint_held_during_quiet_then_resumes():
    ctl, client = _make_control()

    async def run():
        ctl._last_switch = monotonic()      # a switch just happened
        await ctl._write_setpoint(10)
        held = list(client.writes)
        ctl._last_switch = monotonic() - 25  # quiet period (20 s) elapsed
        await ctl._write_setpoint(10)
        return held, client.writes

    held, after = asyncio.run(run())
    assert held == []                          # no 5004 write during quiet period
    assert ("set_current_a", 10) in after      # resumes after the quiet period


def test_plugging_in_rewrites_zero_when_charging_is_switched_off():
    """Reported bug: with Charging off, plugging in made the car charge at the
    wallbox minimum. The wallbox sets its own current for a new session, and our
    cached last-setpoint (0) made the loop skip the rewrite."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.charging_enabled = False
    ctl._last_setpoint = 0        # we wrote 0 while no car was attached
    ctl._was_connected = False

    data = _data(phase_switch_raw=1, state=2)  # vehicle connected + charging

    asyncio.run(ctl.async_apply(data))

    # 0 A must be written again, even though the target did not change
    assert ("set_current_a", 0) in client.writes
