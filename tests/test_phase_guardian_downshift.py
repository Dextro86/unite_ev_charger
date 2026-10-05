"""Tests for trede 1 (wish guardian) and trede 2 (3->1 downshift mirror).

Guardian: mid-session drift between evcc's wish and register 405 is rewritten
to the wish after 3 consecutive polls - never invents a wish, never disrupts.
Downshift: 1P requested but the car still drawing 3P gets the same
observe-pause-resume sequence as the 1->3 recovery (opt-in, one per request).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import uec.controller as controller_module
from uec.controller import ChargeControl
from uec.models import WallboxData

# Neutralise the hardcoded settle sleep so full-sequence tests are instant.
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

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


def _control(**overrides):
    opts = {
        "control_mode": "external",
        "min_current": 6,
        "max_current": 16,
        "phase_recovery_enabled": True,
        "phase_recovery_observe": 0,
        "phase_recovery_dwell": 0,
    }
    opts.update(overrides)
    client = FakeClient()
    coord = FakeCoordinator(client)
    ctl = ChargeControl(object(), SimpleNamespace(options=opts), coord)
    return ctl, client, coord


def _charging_1p() -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = 2  # charging
    d.cable_state_raw = 2         # connected
    d.phase_switch_raw = 0        # register: 1-phase
    d.current_l1_a = 15.0
    return d


def _charging_3p() -> WallboxData:
    d = _charging_1p()
    d.phase_switch_raw = 1        # register: 3-phase
    d.current_l2_a = 15.0
    d.current_l3_a = 15.0
    return d


# --- trede 2: downshift gate --------------------------------------------------
def test_downshift_gate_needs_stuck_3p_and_opt_in() -> None:
    ctl, _, _ = _control()
    assert ctl._should_start_downshift(_charging_3p()) is True
    ctl_off, _, _ = _control(phase_recovery_enabled=False)
    assert ctl_off._should_start_downshift(_charging_3p()) is False
    assert ctl._should_start_downshift(_charging_1p()) is False
    idle = _charging_3p()
    idle.charge_point_state_raw = 1  # connected, not charging
    assert ctl._should_start_downshift(idle) is False


def test_downshift_full_sequence_pauses_and_resumes() -> None:
    async def run():
        ctl, client, coord = _control()
        coord.data = _charging_3p()
        ctl._requested_phase = "1"
        await ctl.async_external_set_current(16)
        await ctl.async_external_set_phase(1)  # triggers downshift observe(0)
        await ctl._recovery_task               # dwell(0) to completion
        await ctl.async_shutdown()
        return client.writes, ctl._downshift_attempted

    writes, attempted = asyncio.run(run())
    assert attempted is True
    assert ("phase_switch", 0) in writes      # the 1P command itself
    assert ("set_current_a", 0) in writes     # the re-negotiation pause
    assert writes[-1] == ("set_current_a", 16)  # resumed with evcc's intent


def test_downshift_latch_one_per_1p_request() -> None:
    async def run():
        ctl, _, coord = _control()
        coord.data = _charging_3p()
        await ctl.async_external_set_current(16)
        await ctl.async_external_set_phase(1)
        await ctl._recovery_task
        first = ctl._recovery_task
        await ctl.async_external_set_phase(1)  # same wish again
        second = ctl._recovery_task
        await ctl.async_shutdown()
        return first is second

    assert asyncio.run(run()) is True


# --- trede 1: wish guardian ---------------------------------------------------
def test_guardian_rewrites_persistent_drift_to_wish() -> None:
    ctl, client, _ = _control()
    ctl._requested_phase = "1"
    data = _charging_3p()  # 405 = 1 (3P) while wish is 1P
    asyncio.run(ctl._guard_phase_setting(data))
    asyncio.run(ctl._guard_phase_setting(data))
    assert ("phase_switch", 0) not in client.writes  # patience: 2 polls, no write
    asyncio.run(ctl._guard_phase_setting(data))
    assert ("phase_switch", 0) in client.writes      # 3rd consecutive poll writes


def test_guardian_quiet_when_converged_or_wish_unknown() -> None:
    ctl, client, _ = _control()
    ctl._requested_phase = "1"
    for _ in range(5):
        asyncio.run(ctl._guard_phase_setting(_charging_1p()))
    assert client.writes == []
    ctl._requested_phase = None
    for _ in range(5):
        asyncio.run(ctl._guard_phase_setting(_charging_3p()))
    assert client.writes == []


def test_guardian_runs_mid_session_via_apply() -> None:
    async def run():
        ctl, client, coord = _control()
        ctl._requested_phase = "1"
        ctl._was_connected = True  # already in session: no just_connected path
        coord.data = _charging_3p()
        for _ in range(3):
            await ctl.async_apply(coord.data)
        return client.writes

    assert ("phase_switch", 0) in asyncio.run(run())
