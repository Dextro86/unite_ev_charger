"""Tests for the optional adaptive 3->1 phase downshift.

Mirror of the 1->3 recovery for the opposite direction: when 1-phase is
requested but the car keeps drawing all three phases, force a re-negotiation.
The method is selectable (pause / webui / hybrid). Timers are 0 here so the
whole sequence runs instantly.
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


def test_should_start_downshift_gating():
    ctl, _, _ = _control()
    assert ctl._should_start_downshift(_charging_3p()) is True

    disabled, _, _ = _control(phase_downshift_enabled=False)
    assert disabled._should_start_downshift(_charging_3p()) is False

    not_charging = _charging_3p()
    not_charging.charge_point_state_raw = 1
    assert ctl._should_start_downshift(not_charging) is False

    assert ctl._should_start_downshift(_charging_1p()) is False  # already 1-phase

    ctl._downshift_attempted = True
    assert ctl._should_start_downshift(_charging_3p()) is False  # latched


def test_disabled_is_pure_passthrough():
    ctl, client, coord = _control(phase_downshift_enabled=False)
    coord.data = _charging_3p()
    asyncio.run(ctl.async_external_set_phase(1))
    assert client.writes == [("phase_switch", 0)]
    assert ctl.recovery_active is False


def test_phase1_while_charging_3p_starts_downshift():
    ctl, client, coord = _control(phase_downshift_observe=30, phase_downshift_dwell=30)
    coord.data = _charging_3p()

    async def run():
        await ctl.async_external_set_phase(1)
        await asyncio.sleep(0)  # let the observer task reach its first await
        snapshot = (list(client.writes), ctl.recovery_active, ctl.recovery_status)
        await ctl.async_shutdown()  # cancel the observing task
        return snapshot

    writes, active, status = asyncio.run(run())
    assert writes == [("phase_switch", 0)]  # live 405=1 written immediately
    assert active is True
    assert status == "observing_1p"


def test_latch_resets_on_phase3_request():
    ctl, _, coord = _control()
    coord.data = _charging_1p()
    ctl._downshift_attempted = True
    asyncio.run(ctl.async_external_set_phase(3))
    assert ctl._downshift_attempted is False


def test_latch_resets_on_disconnect():
    ctl, _, _ = _control()
    ctl._downshift_attempted = True
    ctl._was_connected = True
    asyncio.run(ctl.async_apply(WallboxData()))  # not connected -> disconnect edge
    assert ctl._downshift_attempted is False


def test_pause_method_resumes_evcc_intent():
    ctl, client, coord = _control(phase_downshift_method="pause")
    coord.data = _charging_3p()

    async def run():
        await ctl.async_external_set_current(16)  # evcc intent = 16 A
        await ctl.async_external_set_phase(1)     # triggers downshift
        await ctl._recovery_task                  # observe(0)+dwell(0) to completion
        return list(client.writes), ctl.recovery_status, coord.resync_calls

    writes, status, resyncs = asyncio.run(run())
    assert ("phase_switch", 0) in writes         # live phase write
    assert ("set_current_a", 0) in writes        # forced pause
    assert writes[-1] == ("set_current_a", 16)   # resumed to evcc's intent
    assert resyncs == 0                           # pause method never touches the web UI
    assert status == "complete"


def test_webui_method_calls_resync():
    ctl, client, coord = _control(phase_downshift_method="webui")
    coord.data = _charging_3p()

    async def run():
        await ctl.async_external_set_phase(1)
        await ctl._recovery_task
        return list(client.writes), coord.resync_calls, ctl.recovery_status

    writes, resyncs, status = asyncio.run(run())
    assert ("phase_switch", 0) in writes
    assert ("set_current_a", 0) not in writes  # webui method does not pause
    assert resyncs == 1
    assert status == "complete"


def test_hybrid_falls_back_to_webui_when_still_3p():
    ctl, client, coord = _control(phase_downshift_method="hybrid")
    coord.data = _charging_3p()  # stays 3-phase after the pause -> escalate to web UI

    async def run():
        await ctl.async_external_set_current(16)
        await ctl.async_external_set_phase(1)
        await ctl._recovery_task
        return list(client.writes), coord.resync_calls, ctl.recovery_status

    writes, resyncs, status = asyncio.run(run())
    assert ("set_current_a", 0) in writes  # paused first
    assert resyncs == 1                     # then escalated to the web UI
    assert status == "complete"
