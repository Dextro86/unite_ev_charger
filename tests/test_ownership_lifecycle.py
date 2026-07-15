"""EMS ownership lease tests."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed
import uec.coordinator as coordinator_module
from uec import registers as R
from uec import integration
from uec.coordinator import OwnershipState, WebastoCoordinator
from uec.modbus import WebastoModbusError


class FakeConfigEntries:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    def async_update_entry(self, entry, *, data) -> None:
        entry.data = data
        self.events.append(("persist", dict(data)))


class FakeStore:
    def __init__(self, events: list[tuple], loaded: dict | None = None) -> None:
        self.events = events
        self.loaded = loaded

    async def async_load(self) -> dict | None:
        self.events.append(("load_journal",))
        return self.loaded

    async def async_save(self, data: dict) -> None:
        self.loaded = dict(data)
        self.events.append(("durable", dict(data)))


class FakeClient:
    def __init__(self, values: dict[str, int], events: list[tuple]) -> None:
        self.values = dict(values)
        self.events = events
        self.connected = True
        self.fail_read: str | None = None
        self.fail_write_once: str | None = None
        self.fail_write: str | None = None
        self.pause_next_setpoint = False
        self.slow_operations = False
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()

    async def read_register(self, register) -> int:
        if self.slow_operations:
            await asyncio.sleep(0)
        name = register.name
        if self.fail_read == name:
            raise WebastoModbusError(f"cannot read {name}")
        value = self.values[name]
        self.events.append(("read", name, value))
        return value

    async def write_register(self, register, value: int) -> None:
        if self.slow_operations:
            await asyncio.sleep(0)
        name = register.name
        self.events.append(("write", name, value))
        if name == R.SET_CURRENT_A.name and self.pause_next_setpoint:
            self.pause_next_setpoint = False
            self.write_started.set()
            await self.allow_write.wait()
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
    coordinator._ownership_store = FakeStore(events)
    coordinator.controller = FakeController()
    coordinator.failsafe_configured = None
    return coordinator, coordinator.client, events


def _state(coordinator: WebastoCoordinator) -> str:
    state = coordinator.ownership_state
    return state.value if hasattr(state, "value") else str(state)


def integration_config_flow():
    """Load the real config flow against the smallest HA test double."""
    import importlib
    import sys
    import types

    config_entries = sys.modules["homeassistant.config_entries"]

    class ConfigFlow:
        def __init_subclass__(cls, **_kwargs) -> None:
            return None

        async def async_set_unique_id(self, _unique_id) -> None:
            return None

        def _abort_if_unique_id_configured(self) -> None:
            return None

        def async_create_entry(self, *, title, data):
            return {"title": title, "data": data}

    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigFlowResult = dict
    config_entries.OptionsFlow = object
    sys.modules["homeassistant.const"].CONF_NAME = "name"
    sys.modules["homeassistant.core"].callback = lambda function: function

    voluptuous = types.ModuleType("voluptuous")
    sys.modules.setdefault("voluptuous", voluptuous)

    selector = types.ModuleType("homeassistant.helpers.selector")

    class Selector:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    selector.EntitySelector = Selector
    selector.EntitySelectorConfig = Selector
    sys.modules["homeassistant.helpers.selector"] = selector
    sys.modules["homeassistant.helpers"].selector = selector

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda _hass: None
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client

    module = importlib.import_module("uec.config_flow")
    return module.UniteConfigFlow()


def test_missing_automatic_control_key_defaults_off():
    coordinator, _client, _events = _coordinator(data={})
    assert coordinator.automatic_control_requested is False


def test_new_entry_persists_automatic_control_off(monkeypatch):
    flow = integration_config_flow()
    result = asyncio.run(
        flow.async_step_user(
            {
                "name": "Unite",
                "host": "charger",
                "port": 502,
                "unit_id": 255,
            }
        )
    )
    assert result["data"]["automatic_control"] is False


def test_activation_persists_complete_snapshot_before_first_write():
    coordinator, _client, events = _coordinator()

    asyncio.run(coordinator.async_activate())

    assert events[:4] == [
        ("read", R.SET_CURRENT_A.name, 20),
        ("read", R.FAILSAFE_CURRENT_A.name, 12),
        ("read", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("read", R.PHASE_SWITCH.name, 0),
    ]
    durable_index = next(i for i, event in enumerate(events) if event[0] == "durable")
    persist_index = next(i for i, event in enumerate(events) if event[0] == "persist")
    write_index = next(i for i, event in enumerate(events) if event[0] == "write")
    assert durable_index < persist_index < write_index
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
        ("write", R.ALIVE.name, 1),
        ("write", R.FAILSAFE_CURRENT_A.name, 12),
        ("write", R.ALIVE.name, 1),
        ("write", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("write", R.ALIVE.name, 1),
        ("write", R.PHASE_SWITCH.name, 0),
        ("write", R.ALIVE.name, 1),
        ("write", R.ALIVE.name, 1),
        ("write", R.ALIVE.name, 1),
        ("write", R.ALIVE.name, 1),
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


def test_slow_restoration_refreshes_alive_before_each_write_and_read():
    coordinator, client, events = _coordinator()
    asyncio.run(coordinator.async_activate())
    events.clear()
    client.slow_operations = True

    assert asyncio.run(coordinator.async_suspend(preserve_requested=False)) is True

    restored = {
        R.SET_CURRENT_A.name,
        R.FAILSAFE_CURRENT_A.name,
        R.FAILSAFE_TIMEOUT_S.name,
        R.PHASE_SWITCH.name,
    }
    operations = [
        index
        for index, event in enumerate(events)
        if event[0] in {"write", "read"} and event[1] in restored
    ]
    assert operations
    for index in operations:
        assert events[index - 1] == ("write", R.ALIVE.name, 1)


def test_owned_write_race_finishes_before_suspension_restores_originals():
    async def exercise() -> list[tuple]:
        coordinator, client, events = _coordinator()
        await coordinator.async_activate()
        client.pause_next_setpoint = True
        control = asyncio.create_task(
            coordinator.async_write_owned(R.SET_CURRENT_A, 16)
        )
        await client.write_started.wait()
        suspend = asyncio.create_task(
            coordinator.async_suspend(preserve_requested=False)
        )
        await asyncio.sleep(0)
        client.allow_write.set()
        await control
        assert await suspend is True
        return events

    writes = [
        event
        for event in asyncio.run(exercise())
        if event[0] == "write" and event[1] != R.ALIVE.name
    ]
    assert writes[-4:] == [
        ("write", R.SET_CURRENT_A.name, 20),
        ("write", R.FAILSAFE_CURRENT_A.name, 12),
        ("write", R.FAILSAFE_TIMEOUT_S.name, 45),
        ("write", R.PHASE_SWITCH.name, 0),
    ]


def test_owned_write_after_suspension_is_rejected():
    async def exercise() -> None:
        coordinator, client, _events = _coordinator()
        await coordinator.async_activate()
        client.pause_next_setpoint = True
        suspend = asyncio.create_task(
            coordinator.async_suspend(preserve_requested=False)
        )
        await client.write_started.wait()
        control = asyncio.create_task(
            coordinator.async_write_owned(R.SET_CURRENT_A, 16)
        )
        await asyncio.sleep(0)
        client.allow_write.set()

        with pytest.raises(WebastoModbusError, match="not active"):
            await control
        assert await suspend is True
        assert client.values[R.SET_CURRENT_A.name] == 20

    asyncio.run(exercise())


def test_update_heartbeat_does_not_write_or_reset_state_after_suspension(monkeypatch):
    async def exercise() -> WebastoCoordinator:
        coordinator, client, _events = _coordinator()
        await coordinator.async_activate()
        client.values[R.NUMBER_OF_PHASES.name] = 3
        client.take_new_connection = lambda: False
        coordinator.device = SimpleNamespace(phases_supported=3)

        async def read_telemetry() -> list[int]:
            return []

        async def read_session(*_args) -> list[int]:
            return []

        class SuspendingController(FakeController):
            heartbeat_allowed = True

            async def async_apply(self, _data) -> None:
                assert await coordinator.async_suspend(preserve_requested=True) is True

        coordinator._read_telemetry_block = read_telemetry
        client.read_input_block = read_session
        coordinator.controller = SuspendingController()
        monkeypatch.setattr(
            coordinator_module, "parse_telemetry", lambda _values: SimpleNamespace()
        )
        monkeypatch.setattr(coordinator_module, "apply_session", lambda *_args: None)

        with pytest.raises(UpdateFailed, match="stopped during update"):
            await coordinator._async_update_data()
        return coordinator

    coordinator = asyncio.run(exercise())
    assert coordinator.ownership_state is OwnershipState.SUSPENDED


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


def test_startup_loads_durable_journal_before_deciding_ownership():
    coordinator, _client, events = _coordinator(
        data={"automatic_control": True, "ownership_dirty": False}
    )
    coordinator._ownership_store.loaded = {
        "automatic_control": False,
        "ownership_dirty": True,
        "original_current_limit": 20,
        "original_failsafe_current": 12,
        "original_failsafe_timeout": 45,
        "original_phase_switch": 0,
    }

    asyncio.run(coordinator.async_load_ownership_record())

    assert events[0] == ("load_journal",)
    assert coordinator.automatic_control_requested is False
    assert coordinator.ownership_dirty is True
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


def test_failsafe_programming_is_required_without_dlb():
    coordinator, client, _events = _coordinator()
    coordinator.entry.options["dlb_enabled"] = False
    client.fail_write_once = R.FAILSAFE_TIMEOUT_S.name

    with pytest.raises(WebastoModbusError, match="cannot write"):
        asyncio.run(coordinator.async_activate())

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

    async def exercise() -> None:
        await coordinator.async_activate()
        await coordinator.async_claim_connection()

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        asyncio.run(exercise())

    assert (
        "Claimed charger control: live limit=6 A, failsafe=6 A after 30 s"
        in caplog.text
    )


def test_reconnect_claim_is_rejected_outside_owned_lifecycle():
    coordinator, _client, _events = _coordinator()

    with pytest.raises(WebastoModbusError, match="reconnect rejected"):
        asyncio.run(coordinator.async_claim_connection())


def test_reconnect_callback_direct_writes_run_under_lifecycle_lock():
    coordinator, client, events = _coordinator()

    class LockedReconnectController(FakeController):
        async def async_on_reconnect(self, _phase_raw: int | None) -> None:
            assert coordinator._ownership_lock.locked()
            await coordinator.client.write_register(R.PHASE_SWITCH, 1)

    coordinator.controller = LockedReconnectController()
    asyncio.run(coordinator.async_activate())

    assert ("write", R.PHASE_SWITCH.name, 1) in events
    assert client.values[R.PHASE_SWITCH.name] == 1


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

        async def async_load_ownership_record(self) -> None:
            events.append("load")

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

    assert events == ["load", "controller", "platforms"]


def test_setup_with_control_on_activates_before_first_refresh(monkeypatch):
    hass, entry, events, _bus = _integration_fakes(monkeypatch, requested=True)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    assert events == [
        "load",
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
