"""Ownership lifecycle tests for register 5004."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from uec import registers as R
from uec.coordinator import WebastoCoordinator
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

    def take_new_connection(self) -> bool:
        return True


def _coordinator(*, data: dict, current_limit: int = 20):
    coordinator = WebastoCoordinator.__new__(WebastoCoordinator)
    coordinator.hass = SimpleNamespace(config_entries=FakeConfigEntries())
    coordinator.entry = SimpleNamespace(data=data, options={})
    coordinator.client = FakeClient(current_limit)
    coordinator.controller = None
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


def test_claim_logs_effective_safety_values(caplog):
    coordinator = _coordinator(data={})
    coordinator.entry.options = {
        "dlb_enabled": True,
        "failsafe_current": 6,
        "failsafe_timeout": 30,
    }

    with caplog.at_level(logging.INFO, logger="uec.coordinator"):
        asyncio.run(coordinator.async_claim_connection())

    assert (
        "Claimed charger control: live limit=6 A, failsafe=6 A after 30 s"
        in caplog.text
    )
