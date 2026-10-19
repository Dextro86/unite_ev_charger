"""Tests for "uit = 0 A op de draad" in the built-in controller.

* A session start (just_connected) with charging switched OFF writes 0 A to
  register 5004 immediately - not just invalidating the cache and letting the
  loop confirm it one cycle later.
* With charging switched ON, a session start does nothing extra.
* An explicit switch-off always writes 0 A for real, bypassing the
  "already applied" skip; silent repetitions skip only with matching readback.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from uec.controller import ChargeControl
from uec.modbus import WebastoModbusError
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
    """Skip silent repetitions only when the hardware confirms the target."""
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.manual_current = 10
    ctl.charging_enabled = True
    ctl._last_setpoint = 10           # already on the wire
    ctl._was_connected = True

    data = _data()
    data.set_current_a = 10
    asyncio.run(ctl.async_apply(data))

    assert client.writes == []


@pytest.mark.parametrize(
    "enabled,mode,manual_current,maximum,readback,target",
    [
        (False, "manual", 10, 16, 6, 0),
        (True, "fast", 10, 16, 6, 16),
        (True, "fast", 10, 12, 6, 12),
        (True, "manual", 10, 16, 6, 10),
        (True, "fast", 10, 16, 0, 16),
        (False, "manual", 10, 16, None, 0),
    ],
)
def test_mid_session_readback_drift_reapplies_current_target(
    enabled, mode, manual_current, maximum, readback, target,
) -> None:
    """A later limit reset must not be hidden by the last-write cache."""
    ctl, client = _make_control()
    ctl.cfg = replace(ctl.cfg, max_current=maximum)
    ctl.mode = mode
    ctl.manual_current = manual_current
    ctl.charging_enabled = enabled
    ctl._last_setpoint = target
    ctl._was_connected = True  # not a reconnect: exercise cyclic reconciliation
    data = _data()
    data.set_current_a = readback

    asyncio.run(ctl.async_apply(data))

    assert client.writes == [("set_current_a", target)]
    assert ctl.computed_setpoint == target


def test_matching_readback_does_not_repeat_reconciliation_write() -> None:
    ctl, client = _make_control()
    ctl.mode = "manual"
    ctl.manual_current = 10
    ctl._last_setpoint = 10
    ctl._was_connected = True
    data = _data()
    data.set_current_a = 6
    asyncio.run(ctl.async_apply(data))
    data.set_current_a = 10
    asyncio.run(ctl.async_apply(data))

    assert client.writes == [("set_current_a", 10)]


def test_failed_reconciliation_write_clears_cache_and_retries() -> None:
    ctl, client = _make_control()
    ctl.charging_enabled = False
    ctl._last_setpoint = 0
    ctl._was_connected = True
    data = _data()
    data.set_current_a = 6
    write = client.write_register

    async def fail_once(reg, value):
        raise WebastoModbusError("simulated write failure")

    client.write_register = fail_once
    asyncio.run(ctl.async_apply(data))
    assert ctl._last_setpoint is None
    client.write_register = write
    asyncio.run(ctl.async_apply(data))

    assert client.writes == [("set_current_a", 0)]


def test_external_control_does_not_reconcile_current() -> None:
    ctl, client = _make_control()
    ctl.cfg = replace(ctl.cfg, control_mode="external")
    ctl._last_setpoint = 0
    ctl._was_connected = True
    data = _data()
    data.set_current_a = 6
    asyncio.run(ctl.async_apply(data))

    assert client.writes == []
