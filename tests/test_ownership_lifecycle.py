"""EMS ownership lease tests."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from uec import registers as R
from uec import integration
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


def test_claim_logs_effective_safety_values(caplog):
    coordinator, _client, _events = _coordinator()

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        asyncio.run(coordinator.async_claim_connection())

    assert (
        "Claimed charger control: live limit=6 A, failsafe=6 A after 30 s"
        in caplog.text
    )


class FakeBus:
    def __init__(self) -> None:
        self.callback = None

    def async_listen_once(self, _event_type, callback):
        self.callback = callback
        return lambda: None


def _integration_fakes(monkeypatch, *, requested: bool):
    events: list[str] = []

    class Client:
        def __init__(self, **_kwargs) -> None:
            self.connected = False

        async def async_close(self) -> None:
            events.append("old_close")

    class Coordinator:
        def __init__(self, _hass, entry, client) -> None:
            self.entry = entry
            self.client = client
            self.controller = None

        @property
        def automatic_control_requested(self) -> bool:
            return requested

        @property
        def ownership_dirty(self) -> bool:
            return False

        @property
        def ownership_active(self) -> bool:
            return requested

        async def async_activate(self) -> None:
            events.append("activate")

        async def async_suspend(self, *, preserve_requested: bool) -> bool:
            events.append(f"suspend:{preserve_requested}")
            return True

        async def async_read_device_info(self) -> None:
            events.append("device_info")

        async def async_config_entry_first_refresh(self) -> None:
            events.append("refresh")

    class Controller:
        def __init__(self, _hass, _entry, coordinator) -> None:
            coordinator.controller = self
            events.append("controller")

        async def async_on_reconnect(self, _phase) -> None:
            events.append("old_reconnect")

        async def async_shutdown(self) -> None:
            events.append("old_shutdown")

    class ConfigEntries:
        async def async_forward_entry_setups(self, _entry, _platforms) -> None:
            events.append("platforms")

        async def async_unload_platforms(self, _entry, _platforms) -> bool:
            events.append("platforms_unload")
            return True

    bus = FakeBus()
    hass = SimpleNamespace(config_entries=ConfigEntries(), data={}, bus=bus)
    entry = SimpleNamespace(
        data={
            "host": "charger",
            "port": 502,
            "unit_id": 255,
            "automatic_control": requested,
        },
        options={},
        async_on_unload=lambda _callback: None,
        add_update_listener=lambda _callback: None,
        entry_id="entry",
    )
    monkeypatch.setattr(integration, "WebastoModbus", Client)
    monkeypatch.setattr(integration, "WebastoCoordinator", Coordinator)
    monkeypatch.setattr(integration, "ChargeControl", Controller)
    return hass, entry, events, bus


def test_setup_with_control_off_never_claims_or_polls(monkeypatch):
    hass, entry, events, _bus = _integration_fakes(monkeypatch, requested=False)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    assert events == ["controller", "platforms"]


def test_setup_with_control_on_activates_before_first_refresh(monkeypatch):
    hass, entry, events, _bus = _integration_fakes(monkeypatch, requested=True)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    assert events == [
        "controller",
        "activate",
        "device_info",
        "refresh",
        "platforms",
    ]


def test_unload_suspends_before_platform_teardown(monkeypatch):
    hass, entry, events, _bus = _integration_fakes(monkeypatch, requested=True)
    asyncio.run(integration.async_setup_entry(hass, entry))
    events.clear()

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    assert events == ["suspend:True", "platforms_unload"]


def test_shutdown_listener_suspends_with_requested_state_preserved(monkeypatch):
    hass, entry, events, bus = _integration_fakes(monkeypatch, requested=True)
    asyncio.run(integration.async_setup_entry(hass, entry))
    events.clear()

    assert bus.callback is not None
    asyncio.run(bus.callback(SimpleNamespace()))

    assert events == ["suspend:True"]


def test_version_one_migration_requires_explicit_baseline_confirmation():
    updates: list[tuple[dict, int]] = []

    class ConfigEntries:
        def async_update_entry(self, entry, *, data, version) -> None:
            entry.data = data
            entry.version = version
            updates.append((dict(data), version))

    hass = SimpleNamespace(config_entries=ConfigEntries())
    entry = SimpleNamespace(
        version=1,
        data={"host": "charger", "port": 502, "unit_id": 255},
    )

    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
    assert entry.version == 2
    assert entry.data["baseline_required"] is True
    assert entry.data["automatic_control"] is False
    assert entry.data["ownership_dirty"] is False
    assert updates == [(entry.data, 2)]


def test_confirmed_activation_clears_legacy_marker_after_snapshot_capture():
    coordinator, _client, events = _coordinator(
        data={"baseline_required": True, "automatic_control": False}
    )

    asyncio.run(coordinator.async_activate())

    snapshot_persist = next(
        event[1]
        for event in events
        if event[0] == "persist" and event[1].get("ownership_dirty") is True
    )
    assert "baseline_required" not in snapshot_persist
    assert snapshot_persist["original_current_limit"] == 20
    assert snapshot_persist["original_failsafe_current"] == 12
    assert snapshot_persist["original_failsafe_timeout"] == 45
