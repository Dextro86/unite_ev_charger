"""EMS ownership lease tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from uec import registers as R
from uec.coordinator import WebastoCoordinator
from uec.modbus import WebastoModbusError


class FakeConfigEntries:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    def async_update_entry(self, entry, *, data) -> None:
        entry.data = data
        self.events.append(("persist", dict(data)))


class FakeClient:
    def __init__(self, values: dict[str, int], events: list[tuple]) -> None:
        self.values = dict(values)
        self.events = events
        self.connected = True
        self.fail_read: str | None = None
        self.fail_write_once: str | None = None
        self.fail_write: str | None = None

    async def read_register(self, register) -> int:
        name = register.name
        if self.fail_read == name:
            raise WebastoModbusError(f"cannot read {name}")
        value = self.values[name]
        self.events.append(("read", name, value))
        return value

    async def write_register(self, register, value: int) -> None:
        name = register.name
        self.events.append(("write", name, value))
        if self.fail_write == name:
            raise WebastoModbusError(f"cannot write {name}")
        if self.fail_write_once == name:
            self.fail_write_once = None
            raise WebastoModbusError(f"cannot write {name}")
        self.values[name] = value

    async def async_close(self) -> None:
        self.events.append(("close",))
        self.connected = False

    def take_new_connection(self) -> bool:
        return True


class FakeController:
    async def async_on_reconnect(self, phase_raw: int | None) -> None:
        return None

    async def async_shutdown(self) -> None:
        return None


def _coordinator(
    *,
    values: dict[str, int] | None = None,
    data: dict | None = None,
    phase_control: bool = True,
) -> tuple[WebastoCoordinator, FakeClient, list[tuple]]:
    events: list[tuple] = []
    values = values or {
        R.SET_CURRENT_A.name: 20,
        R.FAILSAFE_CURRENT_A.name: 12,
        R.FAILSAFE_TIMEOUT_S.name: 45,
        R.PHASE_SWITCH.name: 0,
    }
    coordinator = WebastoCoordinator.__new__(WebastoCoordinator)
    coordinator.hass = SimpleNamespace(config_entries=FakeConfigEntries(events))
    coordinator.entry = SimpleNamespace(
        data={"host": "charger", **(data or {})},
        options={
            "dlb_enabled": True,
            "failsafe_current": 6,
            "failsafe_timeout": 30,
            "phase_switching": phase_control,
        },
    )
    coordinator.client = FakeClient(values, events)
    coordinator.controller = FakeController()
    coordinator.failsafe_configured = None
    return coordinator, coordinator.client, events


def _state(coordinator: WebastoCoordinator) -> str:
    state = coordinator.ownership_state
    return state.value if hasattr(state, "value") else str(state)


def test_activation_persists_complete_snapshot_before_first_write():
    coordinator, _client, events = _coordinator()

    asyncio.run(coordinator.async_activate())

    assert events[:4] == [
        ("read", R.SET_CURRENT_A.name, 20),
        ("read", R.FAILSAFE_CURRENT_A.name, 12),
        ("read", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("read", R.PHASE_SWITCH.name, 0),
    ]
    persist_index = next(i for i, event in enumerate(events) if event[0] == "persist")
    write_index = next(i for i, event in enumerate(events) if event[0] == "write")
    assert persist_index < write_index
    assert coordinator.entry.data["ownership_dirty"] is True
    assert coordinator.entry.data["original_current_limit"] == 20
    assert coordinator.entry.data["original_failsafe_current"] == 12
    assert coordinator.entry.data["original_failsafe_timeout"] == 45
    assert coordinator.entry.data["original_phase_switch"] == 0
    assert _state(coordinator) == "active"


def test_disable_restores_verifies_then_closes_and_clears_snapshot():
    coordinator, _client, events = _coordinator()
    asyncio.run(coordinator.async_activate())
    events.clear()

    assert asyncio.run(coordinator.async_suspend(preserve_requested=False)) is True

    writes = [event for event in events if event[0] == "write"]
    assert writes == [
        ("write", R.ALIVE.name, 1),
        ("write", R.SET_CURRENT_A.name, 20),
        ("write", R.FAILSAFE_CURRENT_A.name, 12),
        ("write", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("write", R.PHASE_SWITCH.name, 0),
    ]
    verification_reads = [event[1] for event in events if event[0] == "read"]
    assert verification_reads == [
        R.SET_CURRENT_A.name,
        R.FAILSAFE_CURRENT_A.name,
        R.FAILSAFE_TIMEOUT_S.name,
        R.PHASE_SWITCH.name,
    ]
    assert events.index(("close",)) > max(
        i for i, event in enumerate(events) if event[0] == "read"
    )
    assert coordinator.entry.data["automatic_control"] is False
    assert coordinator.entry.data["ownership_dirty"] is False
    assert "original_current_limit" not in coordinator.entry.data
    assert _state(coordinator) == "suspended"


def test_repeated_cycles_are_idempotent_and_capture_fresh_values():
    coordinator, client, events = _coordinator()
    asyncio.run(coordinator.async_activate())
    active_event_count = len(events)
    asyncio.run(coordinator.async_activate())
    assert len(events) == active_event_count

    asyncio.run(coordinator.async_suspend(preserve_requested=False))
    suspended_event_count = len(events)
    asyncio.run(coordinator.async_suspend(preserve_requested=False))
    assert len(events) == suspended_event_count

    client.values.update(
        {
            R.SET_CURRENT_A.name: 24,
            R.FAILSAFE_CURRENT_A.name: 14,
            R.FAILSAFE_TIMEOUT_S.name: 60,
            R.PHASE_SWITCH.name: 1,
        }
    )
    client.connected = True
    asyncio.run(coordinator.async_activate())
    assert coordinator.entry.data["original_current_limit"] == 24
    assert coordinator.entry.data["original_failsafe_current"] == 14
    assert coordinator.entry.data["original_failsafe_timeout"] == 60
    assert coordinator.entry.data["original_phase_switch"] == 1


def test_snapshot_read_failure_performs_no_writes():
    coordinator, client, events = _coordinator()
    client.fail_read = R.FAILSAFE_TIMEOUT_S.name

    with pytest.raises(WebastoModbusError, match="cannot read"):
        asyncio.run(coordinator.async_activate())

    assert not any(event[0] == "write" for event in events)
    assert coordinator.entry.data.get("ownership_dirty") is not True
    assert _state(coordinator) == "error"


def test_initialization_write_failure_rolls_back_snapshot():
    coordinator, client, events = _coordinator()
    client.fail_write_once = R.FAILSAFE_TIMEOUT_S.name

    with pytest.raises(WebastoModbusError, match="cannot write"):
        asyncio.run(coordinator.async_activate())

    restored = [
        event
        for event in events
        if event[0] == "write"
        and event[1] in {
            R.SET_CURRENT_A.name,
            R.FAILSAFE_CURRENT_A.name,
            R.FAILSAFE_TIMEOUT_S.name,
            R.PHASE_SWITCH.name,
        }
    ]
    assert restored[-4:] == [
        ("write", R.SET_CURRENT_A.name, 20),
        ("write", R.FAILSAFE_CURRENT_A.name, 12),
        ("write", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("write", R.PHASE_SWITCH.name, 0),
    ]
    assert coordinator.entry.data["ownership_dirty"] is False
    assert _state(coordinator) == "suspended"


def test_restore_failure_keeps_dirty_snapshot_and_retry_completes():
    coordinator, client, _events = _coordinator()
    asyncio.run(coordinator.async_activate())
    client.fail_write = R.FAILSAFE_CURRENT_A.name

    assert asyncio.run(coordinator.async_suspend(preserve_requested=False)) is False
    assert coordinator.entry.data["automatic_control"] is False
    assert coordinator.entry.data["ownership_dirty"] is True
    assert _state(coordinator) == "error"
    assert client.connected is True

    client.fail_write = None
    assert asyncio.run(coordinator.async_suspend(preserve_requested=False)) is True
    assert coordinator.entry.data["ownership_dirty"] is False
    assert _state(coordinator) == "suspended"
