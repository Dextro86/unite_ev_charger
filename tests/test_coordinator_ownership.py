"""Ownership lifecycle tests for register 5004."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from uec import registers as R
from uec.coordinator import WebastoCoordinator
from uec import integration
from uec.modbus import WebastoModbusError

CONF_ORIGINAL_CURRENT_LIMIT = "original_current_limit"


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def async_update_entry(self, entry, *, data) -> None:
        entry.data = data
        self.updates.append(data)


class FakeClient:
    def __init__(self, current_limit: int = 20) -> None:
        self.current_limit = current_limit
        self.reads: list[str] = []
        self.writes: list[tuple[str, int]] = []
        self.write_error: WebastoModbusError | None = None

    async def read_register(self, register) -> int:
        self.reads.append(register.name)
        return self.current_limit

    async def write_register(self, register, value: int) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((register.name, value))


def _coordinator(*, data: dict, current_limit: int = 20):
    coordinator = WebastoCoordinator.__new__(WebastoCoordinator)
    coordinator.hass = SimpleNamespace(config_entries=FakeConfigEntries())
    coordinator.entry = SimpleNamespace(data=data)
    coordinator.client = FakeClient(current_limit)
    return coordinator


def test_capture_reads_and_persists_original_limit_once(caplog):
    coordinator = _coordinator(data={"host": "charger"}, current_limit=20)

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        result = asyncio.run(coordinator.async_capture_original_current_limit())

    assert result == 20
    assert coordinator.client.reads == [R.SET_CURRENT_A.name]
    assert coordinator.entry.data[CONF_ORIGINAL_CURRENT_LIMIT] == 20
    assert coordinator.hass.config_entries.updates == [
        {"host": "charger", CONF_ORIGINAL_CURRENT_LIMIT: 20}
    ]
    assert "Captured original charging-current limit from register 5004: 20 A" in caplog.text


def test_capture_never_overwrites_persisted_original_limit(caplog):
    coordinator = _coordinator(
        data={CONF_ORIGINAL_CURRENT_LIMIT: 24},
        current_limit=6,
    )

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        result = asyncio.run(coordinator.async_capture_original_current_limit())

    assert result == 24
    assert coordinator.client.reads == []
    assert coordinator.hass.config_entries.updates == []
    assert "Using stored original charging-current limit: 24 A" in caplog.text


def test_capture_rejects_impossible_original_limit():
    coordinator = _coordinator(data={}, current_limit=99)

    with pytest.raises(WebastoModbusError, match="outside 0..32 A"):
        asyncio.run(coordinator.async_capture_original_current_limit())

    assert coordinator.hass.config_entries.updates == []


def test_restore_writes_persisted_original_limit(caplog):
    coordinator = _coordinator(data={CONF_ORIGINAL_CURRENT_LIMIT: 20})

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        restored = asyncio.run(coordinator.async_restore_original_current_limit())

    assert restored is True
    assert coordinator.client.writes == [(R.SET_CURRENT_A.name, 20)]
    assert "Restored original charging-current limit to register 5004: 20 A" in caplog.text


def test_restore_failure_is_best_effort(caplog):
    coordinator = _coordinator(data={CONF_ORIGINAL_CURRENT_LIMIT: 20})
    coordinator.client.write_error = WebastoModbusError("offline")

    with caplog.at_level(logging.WARNING, logger="uec.coordinator"):
        restored = asyncio.run(coordinator.async_restore_original_current_limit())

    assert restored is False
    assert coordinator.client.writes == []
    assert "Could not restore original charging-current limit to 20 A: offline" in caplog.text


def test_setup_captures_original_before_claiming_connection(monkeypatch):
    events: list[str] = []

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def async_close(self) -> None:
            events.append("close")

    class Coordinator:
        def __init__(self, _hass, _entry, client) -> None:
            self.client = client

        async def async_capture_original_current_limit(self) -> None:
            events.append("capture")

        async def async_claim_connection(self) -> None:
            events.append("claim")

        async def async_read_device_info(self) -> None:
            events.append("device_info")

        async def async_config_entry_first_refresh(self) -> None:
            events.append("refresh")

    class Controller:
        def __init__(self, _hass, _entry, _coordinator) -> None:
            pass

        async def async_on_reconnect(self, _phase) -> None:
            events.append("controller")

    class ConfigEntries:
        async def async_forward_entry_setups(self, _entry, _platforms) -> None:
            events.append("platforms")

    hass = SimpleNamespace(config_entries=ConfigEntries(), data={})
    entry = SimpleNamespace(
        data={"host": "charger", "port": 502, "unit_id": 255},
        async_on_unload=lambda _callback: None,
        add_update_listener=lambda _callback: None,
        entry_id="entry",
    )
    monkeypatch.setattr(integration, "WebastoModbus", Client)
    monkeypatch.setattr(integration, "WebastoCoordinator", Coordinator)
    monkeypatch.setattr(integration, "ChargeControl", Controller)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    assert events.index("capture") < events.index("claim")


def test_unload_restores_original_before_closing_connection():
    events: list[str] = []

    class ConfigEntries:
        async def async_unload_platforms(self, _entry, _platforms) -> bool:
            return True

    class Controller:
        async def async_shutdown(self) -> None:
            events.append("shutdown")

    class Client:
        async def async_close(self) -> None:
            events.append("close")

    class Coordinator:
        controller = Controller()
        client = Client()

        async def async_restore_original_current_limit(self) -> bool:
            events.append("restore")
            return True

    entry = SimpleNamespace(entry_id="entry")
    hass = SimpleNamespace(
        config_entries=ConfigEntries(),
        data={"unite_ev_charger": {"entry": Coordinator()}},
    )

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert events == ["shutdown", "restore", "close"]
