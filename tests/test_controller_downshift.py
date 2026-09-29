"""Tests for the optional adaptive 3->1 phase downshift.

When 1-phase is requested but the car keeps drawing all three phases, a
continuous watcher (running every poll, both modes) detects the sustained
mismatch and forces a re-negotiation via the configured method
(pause / webui / hybrid). Timers are 0 here so the sequence runs instantly.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import uec.controller as controller_module
from uec.controller import ChargeControl
from uec.models import WallboxData

# Neutralise the hardcoded settle sleep so the full-sequence tests are instant.
controller_module.PHASE_RECOVERY_SETTLE_S = 0


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
        self.resync_calls = 0

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass

    async def async_force_phase_resync(self) -> str:
        self.resync_calls += 1
        return "test"


def _control(**overrides):
    opts = {
        "control_mode": "external",
        "min_current": 6,
        "max_current": 16,
        "phase_downshift_enabled": True,
        "phase_downshift_method": "pause",
        "phase_downshift_observe": 0,
        "phase_downshift_dwell": 0,
    }
    opts.update(overrides)
    client = FakeClient()
    coord = FakeCoordinator(client)
    ctl = ChargeControl(object(), SimpleNamespace(options=opts), coord)
    return ctl, client, coord


def _charging_3p(set_current: int = 16) -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = 2  # charging
    d.cable_state_raw = 2         # connected
    d.phase_switch_raw = 0        # register: 1-phase requested
    d.current_l1_a = 15.0         # but the car still draws all three phases
    d.current_l2_a = 15.0
    d.current_l3_a = 15.0
    d.set_current_a = set_current
    return d


def _charging_1p(set_current: int = 16) -> WallboxData:
    d = _charging_3p(set_current)
    d.current_l2_a = 0.0
    d.current_l3_a = 0.0
    return d


def test_wanted_phase():
    ext, _, _ = _control()
    ext._requested_phase = "1"
    assert ext._wanted_phase() == 1
    ext._requested_phase = "3"
    assert ext._wanted_phase() == 3
    ext._requested_phase = None
    assert ext._wanted_phase() is None

    intern, _, _ = _control(control_mode="internal", phase_switching=True)
    intern.phase_preference = "1"
    assert intern._wanted_phase() == 1
    intern.phase_preference = "auto"
    assert intern._wanted_phase() is None


def test_watcher_ignores_transient_and_disabled():
    # Only 1 phase drawn -> not a mismatch, timer stays clear.
    ctl, _, _ = _control()
    ctl._requested_phase = "1"
    ctl._maybe_start_phase_fix(_charging_1p())
    assert ctl._phase_mismatch_since is None
    assert ctl.recovery_active is False

    # 3-phase drawn but 3-phase wanted -> not a mismatch.
    ctl._requested_phase = "3"
    ctl._maybe_start_phase_fix(_charging_3p())
    assert ctl._phase_mismatch_since is None

    # Disabled -> never arms.
    off, _, _ = _control(phase_downshift_enabled=False)
    off._requested_phase = "1"
    off._maybe_start_phase_fix(_charging_3p())
    assert off._phase_mismatch_since is None


def test_watcher_needs_two_polls_then_starts():
    ctl, client, coord = _control(phase_downshift_method="pause")
    coord.data = _charging_3p()

    async def run():
        await ctl.async_external_set_phase(1)     # sets requested_phase = "1"
        assert client.writes == [("phase_switch", 0)]  # request itself does not fix
        await ctl.async_external_set_current(16)  # evcc intent
        ctl._maybe_start_phase_fix(coord.data)    # arms the mismatch timer
        armed = ctl._phase_mismatch_since is not None and not ctl.recovery_active
        ctl._maybe_start_phase_fix(coord.data)    # observe(0) elapsed -> starts
        await ctl._recovery_task
        return armed, list(client.writes), ctl.recovery_status

    armed, writes, status = asyncio.run(run())
    assert armed is True
    assert ("set_current_a", 0) in writes        # forced pause
    assert writes[-1] == ("set_current_a", 16)   # resumed to evcc intent
    assert status == "complete"


def test_pause_method_no_webui():
    ctl, client, coord = _control(phase_downshift_method="pause")
    coord.data = _charging_3p()

    async def run():
        await ctl.async_external_set_current(16)
        ctl._start_downshift()
        await ctl._recovery_task
        return list(client.writes), coord.resync_calls, ctl.recovery_status

    writes, resyncs, status = asyncio.run(run())
    assert ("set_current_a", 0) in writes
    assert writes[-1] == ("set_current_a", 16)
    assert resyncs == 0
    assert status == "complete"


def test_webui_method_calls_resync_without_pause():
    ctl, client, coord = _control(phase_downshift_method="webui")
    coord.data = _charging_3p()

    async def run():
        ctl._start_downshift()
        await ctl._recovery_task
        return list(client.writes), coord.resync_calls, ctl.recovery_status

    writes, resyncs, status = asyncio.run(run())
    assert ("set_current_a", 0) not in writes  # web UI method does not pause
    assert resyncs == 1
    assert status == "complete"


def test_hybrid_falls_back_to_webui_when_still_3p():
    ctl, client, coord = _control(phase_downshift_method="hybrid")
    coord.data = _charging_3p()  # stays 3-phase after the pause -> escalate

    async def run():
        await ctl.async_external_set_current(16)
        ctl._start_downshift()
        await ctl._recovery_task
        return list(client.writes), coord.resync_calls, ctl.recovery_status

    writes, resyncs, status = asyncio.run(run())
    assert ("set_current_a", 0) in writes  # paused first
    assert resyncs == 1                     # then escalated to the web UI
    assert status == "complete"


def test_latch_blocks_and_rearms_on_wanted_change():
    ctl, _, coord = _control()
    coord.data = _charging_3p()
    ctl._requested_phase = "1"
    ctl._last_wanted_phase = 1        # avoid the re-arm reset
    ctl._downshift_attempted = True
    ctl._maybe_start_phase_fix(coord.data)  # latched -> no arming
    assert ctl._phase_mismatch_since is None
    assert ctl.recovery_active is False

    ctl._requested_phase = "3"              # target change re-arms the fix
    ctl._maybe_start_phase_fix(_charging_1p())
    assert ctl._downshift_attempted is False


def test_latch_resets_on_disconnect():
    ctl, _, _ = _control()
    ctl._downshift_attempted = True
    ctl._was_connected = True
    asyncio.run(ctl.async_apply(WallboxData()))  # not connected -> disconnect edge
    assert ctl._downshift_attempted is False
