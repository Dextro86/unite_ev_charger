"""Tests for the diagnostics event log and the diagnostics version helper.

The ring buffer is pure observability (the coordinator never reads it back to
make decisions), so it is tested in isolation plus through the controller's
405-write / recovery recording.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from uec.const import integration_version
from uec.controller import ChargeControl
from uec.eventlog import MAX_EVENTS, EventLog
from uec.models import WallboxData


# --- ring buffer ------------------------------------------------------------
def test_eventlog_keeps_newest_at_cap() -> None:
    log = EventLog(maxlen=MAX_EVENTS)
    for i in range(MAX_EVENTS + 5):
        log.record("n", str(i))
    assert len(log) == MAX_EVENTS
    # The oldest 5 were evicted; the first retained event is #5.
    assert log.as_list()[0]["detail"] == "5"
    assert log.as_list()[-1]["detail"] == str(MAX_EVENTS + 4)


def test_eventlog_as_list_has_at_kind_detail() -> None:
    log = EventLog()
    log.record("405_write", "evcc 3P (reg=1)")
    entry = log.as_list()[0]
    assert set(entry) == {"at", "kind", "detail"}
    assert entry["kind"] == "405_write"
    assert entry["detail"] == "evcc 3P (reg=1)"
    assert entry["at"]  # ISO-8601 timestamp is populated


def test_eventlog_ordering_is_fifo() -> None:
    log = EventLog()
    log.record("a", "1")
    log.record("b", "2")
    kinds = [e["kind"] for e in log.as_list()]
    assert kinds == ["a", "b"]


def test_eventlog_phase_events_survive_system_flood() -> None:
    log = EventLog()
    log.record("405_write", "evcc 1P (reg=0)")
    for i in range(40):
        log.record("reconnect", f"storm {i}")
    kinds = [e["kind"] for e in log.as_list()]
    assert "405_write" in kinds
    # System bucket keeps only its own newest.
    assert len(log) == 1 + MAX_EVENTS


# --- integration version (diagnostics content) ------------------------------
def test_integration_version_reads_manifest() -> None:
    assert integration_version() == "0.2.2-beta.1"


# --- controller records into the coordinator's log --------------------------
class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    async def write_register(self, reg, value) -> None:
        self.writes.append((reg.name, value))


class RecordingCoordinator:
    def __init__(self) -> None:
        self.client = FakeClient()
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
    opts = {
        "control_mode": "external",
        "min_current": 6,
        "max_current": 16,
        "phase_recovery_enabled": False,
        "phase_recovery_observe": 0,
        "phase_recovery_dwell": 0,
    }
    opts.update(overrides)
    coord = RecordingCoordinator()
    ctl = ChargeControl(object(), SimpleNamespace(options=opts), coord)
    return ctl, coord


def test_405_write_is_recorded_with_who_and_value() -> None:
    ctl, coord = _control()
    asyncio.run(ctl.async_external_set_phase(3))
    events = coord.event_log.as_list()
    assert events[0]["kind"] == "405_write"
    assert events[0]["detail"] == "evcc 3P (reg=1)"


def test_recovery_attempt_is_recorded() -> None:
    ctl, coord = _control(phase_recovery_enabled=True)
    data = WallboxData()
    data.charge_point_state_raw = 2  # charging
    data.cable_state_raw = 2         # connected
    data.phase_switch_raw = 0        # register: 1-phase
    data.current_l1_a = 15.0
    coord.data = data

    asyncio.run(ctl.async_external_set_phase(3))
    asyncio.run(ctl.async_shutdown())
    kinds = [e["kind"] for e in coord.event_log.as_list()]
    assert "405_write" in kinds
    assert "recovery_attempt" in kinds
