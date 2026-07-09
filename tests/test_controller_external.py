"""Controller tests for external (evcc) control mode.

In external mode the controller is a faithful passthrough: async_apply does
nothing, and the direct-write helpers write straight to the wallbox (current
clamped only to the physical 0-32 A register range).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from uec.controller import ChargeControl
from uec.models import WallboxData

EXT_OPTIONS = {"control_mode": "external", "min_current": 6, "max_current": 16}


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


def _ext_control():
    client = FakeClient()
    coordinator = FakeCoordinator(client)
    ctl = ChargeControl(object(), SimpleNamespace(options=EXT_OPTIONS), coordinator)
    return ctl, client


def test_is_external():
    ctl, _ = _ext_control()
    assert ctl.is_external is True


def test_apply_writes_nothing_in_external_mode():
    ctl, client = _ext_control()
    data = WallboxData()
    data.charge_point_state_raw = 2  # charging
    data.cable_state_raw = 2
    asyncio.run(ctl.async_apply(data))
    assert client.writes == []  # passive: no setpoint writes


def test_external_set_current_clamps_to_register_range():
    ctl, client = _ext_control()

    async def run():
        await ctl.async_external_set_current(50)   # above 32 -> clamp
        await ctl.async_external_set_current(-5)   # below 0 -> clamp
        await ctl.async_external_set_current(10)   # passthrough
        return client.writes

    assert asyncio.run(run()) == [
        ("set_current_a", 32),
        ("set_current_a", 0),
        ("set_current_a", 10),
    ]


def test_external_enable_uses_resume_current():
    ctl, client = _ext_control()

    async def run():
        await ctl.async_external_set_current(12)   # remembers 12 as resume
        await ctl.async_external_set_enabled(False)  # -> 0
        await ctl.async_external_set_enabled(True)   # -> 12 again
        return client.writes

    assert asyncio.run(run()) == [
        ("set_current_a", 12),
        ("set_current_a", 0),
        ("set_current_a", 12),
    ]


def test_external_set_phase_is_a_faithful_passthrough():
    """evcc owns phase switching: each command is one direct 405 write, whether
    or not the car is charging. No stop/hold recovery, so evcc's own phase-switch
    handling (incl. its wake-up pulses for cars that hang) stays intact."""
    ctl, client = _ext_control()
    data = WallboxData()
    data.cable_state_raw = 2          # connected
    data.charge_point_state_raw = 2   # charging on a single phase
    data.phase_switch_raw = 0
    data.current_l1_a = 16.0
    ctl.coordinator.data = data

    async def run():
        await ctl.async_external_set_phase(3)
        await ctl.async_external_set_phase(1)
        return client.writes

    # Exactly one register write per command, straight through.
    assert asyncio.run(run()) == [("phase_switch", 1), ("phase_switch", 0)]
