"""Failsafe ownership-handshake tests."""
from __future__ import annotations

import asyncio

import pytest

from uec.modbus import WebastoModbusError
from uec.safety import program_failsafe


class FakeClient:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.writes: list[tuple[str, int]] = []

    async def write_register(self, register, value: int) -> None:
        self.writes.append((register.name, value))
        if register.name == self.fail_on:
            raise WebastoModbusError("rejected")


def test_failsafe_current_is_written_before_timeout():
    client = FakeClient()

    result = asyncio.run(
        program_failsafe(client, failsafe_current_a=6, failsafe_timeout_s=30)
    )

    assert result is True
    assert client.writes == [
        ("failsafe_current_a", 6),
        ("failsafe_timeout_s", 30),
    ]


def test_failsafe_programming_failure_propagates():
    client = FakeClient(fail_on="failsafe_current_a")

    with pytest.raises(WebastoModbusError):
        asyncio.run(program_failsafe(client, required=True))

    assert client.writes == [("failsafe_current_a", 6)]


def test_optional_failsafe_failure_is_reported_without_blocking_other_modes():
    client = FakeClient(fail_on="failsafe_current_a")

    result = asyncio.run(program_failsafe(client))

    assert result is False
