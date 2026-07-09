"""Tests for the optional adaptive 1->3 phase recovery.

The recovery is opt-in. When enabled it first writes 405=3 and observes; only if
the car is still physically single-phase does it force a long 0 A pause so the
car re-negotiates (no second 405 write). Timers are set to 0 here so the whole
sequence runs instantly.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import uec.controller as controller_module
from uec.controller import ChargeControl
from uec.models import WallboxData

# Neutralise the hardcoded settle sleep so the full-sequence test is instant.
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


def _charging_1p(set_current: int = 16) -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = 2  # charging
    d.cable_state_raw = 2         # connected
    d.phase_switch_raw = 0        # register: 1-phase
    d.current_l1_a = 15.0         # only L1 drawing -> genuinely single phase
    d.set_current_a = set_current
    return d


def _charging_3p() -> WallboxData:
    d = _charging_1p()
    d.phase_switch_raw = 1
    d.current_l2_a = 15.0
    d.current_l3_a = 15.0
    return d


def test_measured_phase_detection():
    assert ChargeControl._measured_single_phase(_charging_1p()) is True
    assert ChargeControl._measured_three_phase(_charging_1p()) is False
    assert ChargeControl._measured_three_phase(_charging_3p()) is True
    assert ChargeControl._measured_single_phase(_charging_3p()) is False


def test_should_start_recovery_gating():
    ctl, _, _ = _control()
    assert ctl._should_start_recovery(_charging_1p()) is True

    disabled, _, _ = _control(phase_recovery_enabled=False)
    assert disabled._should_start_recovery(_charging_1p()) is False

    not_charging = _charging_1p()
    not_charging.charge_point_state_raw = 1
    assert ctl._should_start_recovery(not_charging) is False

    assert ctl._should_start_recovery(_charging_3p()) is False  # already 3-phase

    ctl._recovery_attempted = True
    assert ctl._should_start_recovery(_charging_1p()) is False  # latched


def test_disabled_is_pure_passthrough():
    ctl, client, coord = _control(phase_recovery_enabled=False)
    coord.data = _charging_1p()
    asyncio.run(ctl.async_external_set_phase(3))
    assert client.writes == [("phase_switch", 1)]
    assert ctl.recovery_active is False


def test_phase3_while_charging_1p_starts_recovery():
    ctl, client, coord = _control(phase_recovery_observe=30, phase_recovery_dwell=30)
    coord.data = _charging_1p()

    async def run():
        await ctl.async_external_set_phase(3)
        await asyncio.sleep(0)  # let the observer task run to its first await
        snapshot = (list(client.writes), ctl.recovery_active, ctl.recovery_status)
        await ctl.async_shutdown()  # cancel the observing task
        return snapshot

    writes, active, status = asyncio.run(run())
    assert writes == [("phase_switch", 1)]  # live 405=3 written immediately
    assert active is True
    assert status == "observing_3p"


def test_buffering_holds_positive_current_but_lets_zero_through():
    ctl, client, _ = _control()
    ctl._buffer_commands = True

    async def run():
        await ctl.async_external_set_current(8)   # positive -> buffered
        buffered = list(client.writes)
        await ctl.async_external_set_current(0)   # stop -> always passes through
        return buffered, list(client.writes), ctl.ext_current_intent

    buffered, after_zero, intent = asyncio.run(run())
    assert buffered == []
    assert after_zero == [("set_current_a", 0)]
    assert intent == 0  # intent tracks evcc's latest command


def test_latch_resets_on_phase1_request():
    ctl, _, _ = _control()
    ctl._recovery_attempted = True
    asyncio.run(ctl.async_external_set_phase(1))
    assert ctl._recovery_attempted is False


def test_latch_resets_on_disconnect():
    ctl, _, _ = _control()
    ctl._recovery_attempted = True
    ctl._was_connected = True
    asyncio.run(ctl.async_apply(WallboxData()))  # not connected -> disconnect edge
    assert ctl._recovery_attempted is False


def test_full_recovery_sequence_resumes_evcc_intent():
    ctl, client, coord = _control(phase_recovery_observe=0, phase_recovery_dwell=0)
    coord.data = _charging_1p()

    async def run():
        await ctl.async_external_set_current(16)  # evcc intent = 16 A
        await ctl.async_external_set_phase(3)     # triggers recovery
        await ctl._recovery_task                  # run observe(0)+dwell(0) to completion
        return list(client.writes), ctl.recovery_status

    writes, status = asyncio.run(run())
    assert ("phase_switch", 1) in writes      # live phase write
    assert ("set_current_a", 0) in writes     # forced pause
    assert writes[-1] == ("set_current_a", 16)  # resumed to evcc's intent, no 2nd 405
    assert ("phase_switch", 1) == [w for w in writes if w[0] == "phase_switch"][-1]
    assert writes.count(("phase_switch", 1)) == 1  # exactly one 405 write
    assert status == "complete"


def test_on_reconnect_external_rewrites_evcc_intent():
    ctl, client, _ = _control()  # external

    async def run():
        await ctl.async_external_set_current(16)  # evcc intent = 16 A
        client.writes.clear()
        await ctl.async_on_reconnect(None)
        return list(client.writes)

    assert asyncio.run(run()) == [("set_current_a", 16)]


def test_on_reconnect_external_writes_zero_without_intent():
    ctl, client, _ = _control()  # external, evcc has not commanded yet
    asyncio.run(ctl.async_on_reconnect(None))
    assert client.writes == [("set_current_a", 0)]  # never charge on our own


def test_on_reconnect_during_recovery_reasserts_zero():
    ctl, client, _ = _control()
    ctl._buffer_commands = True   # a forced pause is in progress
    ctl._current_intent = 16      # evcc still wants 16, but the pause must hold 0
    asyncio.run(ctl.async_on_reconnect(None))
    assert client.writes == [("set_current_a", 0)]


def test_on_reconnect_internal_invalidates_setpoint_cache():
    ctl, client, _ = _control(control_mode="internal")
    ctl._last_setpoint = 10
    asyncio.run(ctl.async_on_reconnect(None))
    assert ctl._last_setpoint is None   # control loop re-writes it this cycle
    assert client.writes == []          # handshake itself writes nothing internally


def test_on_reconnect_reasserts_evcc_phase_after_405_reset():
    """The wallbox resets 405 to its 404 default on disconnect; evcc's requested
    phase must be restored (evcc's own select shows intent, so it won't self-fix)."""
    ctl, client, _ = _control()  # external

    async def run():
        await ctl.async_external_set_phase(3)  # evcc requested 3-phase
        client.writes.clear()
        await ctl.async_on_reconnect(0)  # wallbox reset to default 1-phase (raw 0)
        return list(client.writes)

    # current restored (0, no intent yet) + phase re-asserted to 3-phase (405=1)
    assert asyncio.run(run()) == [("set_current_a", 0), ("phase_switch", 1)]


def test_on_reconnect_skips_phase_write_when_default_matches():
    ctl, client, _ = _control()  # external

    async def run():
        await ctl.async_external_set_phase(3)  # wants 3-phase
        client.writes.clear()
        await ctl.async_on_reconnect(1)  # wallbox default already 3-phase (raw 1)
        return [w for w in client.writes if w[0] == "phase_switch"]

    assert asyncio.run(run()) == []  # no CP blip when the reset default matches


def test_on_reconnect_internal_does_not_write_phase():
    ctl, client, _ = _control(control_mode="internal")
    ctl._requested_phase = "3"  # irrelevant internally; the loop handles the phase
    asyncio.run(ctl.async_on_reconnect(0))
    assert all(w[0] != "phase_switch" for w in client.writes)


def test_internal_1p_to_3p_triggers_recovery():
    ctl, client, coord = _control(
        control_mode="internal",
        phase_switching=True,
        phase_recovery_observe=30,
        phase_recovery_dwell=30,
    )
    ctl.mode = "manual"
    ctl.phase_preference = "3"
    data = _charging_1p()
    coord.data = data

    async def run():
        await ctl._manage_phases(data, 230, 0, False)
        await asyncio.sleep(0)
        snapshot = (list(client.writes), ctl.recovery_active)
        await ctl.async_shutdown()
        return snapshot

    writes, active = asyncio.run(run())
    assert ("phase_switch", 1) in writes  # live 405=3 first
    assert active is True                 # recovery started behind it
