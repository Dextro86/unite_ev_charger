"""Tests for "uit = 0 A op de draad" in the built-in controller.

* A session start (just_connected) with charging switched OFF writes 0 A to
  register 5004 immediately - not just invalidating the cache and letting the
  loop confirm it one cycle later.
* With charging switched ON, a session start does nothing extra.
* An explicit switch-off always writes 0 A for real, bypassing the
  "already applied" skip; that skip stays for silent cyclic repetitions.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from uec.controller import ChargeControl
from uec.models import WallboxData

OPTIONS = {
    "default_mode": "manual",
    "min_current": 6,
    "max_current": 16,
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
    return ctl, client


def _data(phase_switch_raw: int = 1, state: int = 2) -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = state  # 2 = charging
    d.cable_state_raw = 2             # vehicle connected
    d.phase_switch_raw = phase_switch_raw
    d.current_l1_a = 10.0
    return d


def test_session_start_with_charging_off_writes_zero_directly() -> None:
    """A new session applies the wallbox's own hardware minimum, so with charging
    off the controller must write 0 A immediately (the loop then confirms it)."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.charging_enabled = False
    ctl._was_connected = False

    asyncio.run(ctl.async_apply(_data()))

    # The direct 0 A write lands first; the loop confirms the same value after.
    assert client.writes[0] == ("set_current_a", 0)
    assert client.writes.count(("set_current_a", 0)) == 2


def test_session_start_with_charging_on_does_nothing_extra() -> None:
    """With charging on, a session start must not add an extra direct write -
    only the loop's normal target write happens."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.manual_current = 10
    ctl.charging_enabled = True
    ctl._was_connected = False

    asyncio.run(ctl.async_apply(_data()))

    assert client.writes == [("set_current_a", 10)]


def test_explicit_off_forces_real_zero_write() -> None:
    """Turning the switch off writes 0 A for real, even when the last written
    setpoint was already 0 (which a silent cycle would otherwise skip)."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.manual_current = 10
    ctl.charging_enabled = True
    ctl._last_setpoint = 0            # a silent cycle already wrote 0 A
    ctl.set_charging_enabled(False)   # explicit switch-off
    ctl._was_connected = True         # not a fresh session

    asyncio.run(ctl.async_apply(_data()))

    assert client.writes == [("set_current_a", 0)]


def test_silent_cycle_still_skips_unchanged_setpoint() -> None:
    """The 'already applied' skip stays for silent cyclic repetitions."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.manual_current = 10
    ctl.charging_enabled = True
    ctl._last_setpoint = 10           # already on the wire
    ctl._was_connected = True

    asyncio.run(ctl.async_apply(_data()))

    assert client.writes == []
