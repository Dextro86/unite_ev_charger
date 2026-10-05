"""Tests for phase re-assert at session start in the charger controller.

External (evcc) mode only: at a new session the wallbox applies its own phase
default, so after re-asserting the charge current the controller writes the
requested phase back only when the freshly read 405 drifted (read-then-write,
idempotent). Internal mode is untouched.

* drift -> one 405 write + a ``phase_reassert_session`` event
* no drift -> no phase write (the coordinator's snapshot read already carries 405)
* no requested phase -> nothing
* recovery active -> skipped
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from uec.controller import ChargeControl
from uec.eventlog import EventLog
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
        self.event_log = EventLog()

    def record_event(self, kind: str, detail: str) -> None:
        self.event_log.record(kind, detail)

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


def _control(**overrides):
    opts = dict(EXT_OPTIONS)
    opts.update(overrides)
    client = FakeClient()
    coord = FakeCoordinator(client)
    ctl = ChargeControl(object(), SimpleNamespace(options=opts), coord)
    return ctl, client, coord


def _data(phase_switch_raw: int) -> WallboxData:
    d = WallboxData()
    d.charge_point_state_raw = 2  # charging
    d.cable_state_raw = 2         # vehicle connected
    d.phase_switch_raw = phase_switch_raw
    return d


def _phase_writes(client: FakeClient) -> list[tuple[str, int]]:
    return [w for w in client.writes if w[0] == "phase_switch"]


def test_session_start_writes_405_when_phase_drifted() -> None:
    ctl, client, coord = _control()
    ctl._requested_phase = "3"

    asyncio.run(ctl.async_apply(_data(phase_switch_raw=0)))  # drifted to 1P

    assert _phase_writes(client) == [("phase_switch", 1)]
    events = [
        e for e in coord.event_log.as_list() if e["kind"] == "phase_reassert_session"
    ]
    assert events
    assert events[-1]["detail"] == "requested=3P measured=0 written=1"


def test_session_start_reads_405_but_writes_nothing_when_matching() -> None:
    ctl, client, _ = _control()
    ctl._requested_phase = "3"

    asyncio.run(ctl.async_apply(_data(phase_switch_raw=1)))  # already 3P

    assert _phase_writes(client) == []  # no 405 write, no CP blip


def test_session_start_without_requested_phase_does_nothing() -> None:
    ctl, client, _ = _control()
    ctl._requested_phase = None  # evcc has never commanded a phase

    asyncio.run(ctl.async_apply(_data(phase_switch_raw=0)))

    assert _phase_writes(client) == []


def test_session_start_skips_phase_reassert_during_recovery() -> None:
    ctl, client, _ = _control()
    ctl._requested_phase = "3"
    # A recovery pause is in progress -> the phase re-assert must be skipped.
    ctl._recovery_task = SimpleNamespace(done=lambda: False)

    asyncio.run(ctl.async_apply(_data(phase_switch_raw=0)))

    assert _phase_writes(client) == []


def test_session_start_internal_mode_never_touches_405() -> None:
    ctl, client, _ = _control(control_mode="internal")
    ctl._requested_phase = "3"

    asyncio.run(ctl.async_apply(_data(phase_switch_raw=0)))

    assert _phase_writes(client) == []  # internal loop handles the phase itself
