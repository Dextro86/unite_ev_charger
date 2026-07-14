"""Lean Modbus TCP client for the Webasto Unite.

Design goals (the fixes that the previous integration lacked):
  * one persistent connection, guarded by a single lock;
  * block reads instead of dozens of single-register reads per cycle;
  * reconnect handled inline without ever sleeping while holding the lock;
  * no per-register reconnect storms.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

from .registers import RegType, RegisterDef, decode_scalar, encode_scalar

_LOGGER = logging.getLogger(__name__)


class WebastoModbusError(RuntimeError):
    """Raised when a Modbus operation cannot be completed."""


@dataclass(slots=True)
class ModbusStats:
    connected: bool = False
    reconnects: int = 0
    read_failures: int = 0
    write_failures: int = 0
    timeouts: int = 0
    alive_failures: int = 0
    last_response_ms: int | None = None
    avg_response_ms: float | None = None  # EWMA, no history buffer
    last_ok: float | None = None
    last_error: str | None = None

    def record_response(self, seconds: float) -> None:
        ms = round(seconds * 1000)
        self.last_response_ms = ms
        self.avg_response_ms = (
            float(ms) if self.avg_response_ms is None else 0.8 * self.avg_response_ms + 0.2 * ms
        )


@dataclass
class WebastoModbus:
    host: str
    port: int = 502
    unit_id: int = 255
    timeout_s: float = 3.0

    _client: AsyncModbusTcpClient | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _unit_kwarg: str | None = field(default=None, init=False, repr=False)
    _new_connection: bool = field(default=False, init=False, repr=False)
    stats: ModbusStats = field(default_factory=ModbusStats, init=False)

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.connected)

    def take_new_connection(self) -> bool:
        """Return True exactly once after a new connection was established.

        The wallbox resets its ownership registers (failsafe + charging current)
        on every new Modbus connection, so the coordinator uses this to re-run
        the required handshake on first connect and after each reconnect.
        """
        if self._new_connection:
            self._new_connection = False
            return True
        return False

    async def async_close(self) -> None:
        async with self._lock:
            self._disconnect_locked()

    # -- public reads/writes -------------------------------------------------
    async def read_input_block(self, address: int, count: int) -> list[int]:
        return await self._read_block(RegType.INPUT, address, count)

    async def read_holding_block(self, address: int, count: int) -> list[int]:
        return await self._read_block(RegType.HOLDING, address, count)

    async def read_block(self, reg_type: RegType, address: int, count: int) -> list[int]:
        """Read a block using an explicitly selected Modbus register area."""
        return await self._read_block(reg_type, address, count)

    async def read_register(self, reg: RegisterDef) -> Any:
        block = await self._read_block(reg.reg_type, reg.address, reg.count)
        return decode_scalar(reg, block)

    async def write_register(self, reg: RegisterDef, value: float | int | bool) -> None:
        if not reg.writable:
            raise WebastoModbusError(f"Register {reg.name} is not writable")
        words = encode_scalar(reg, value)
        async with self._lock:
            try:
                await self._ensure_connected_locked()
                t0 = monotonic()
                if len(words) == 1:
                    resp = await self._request(self._client.write_register, address=reg.address, value=words[0])
                else:
                    resp = await self._request(self._client.write_registers, address=reg.address, values=words)
                self._raise_for_error(resp, f"write {reg.name}")
                self.stats.record_response(monotonic() - t0)
                self._mark_ok()
            except (ModbusException, OSError, asyncio.TimeoutError, WebastoModbusError) as err:
                self.stats.write_failures += 1
                if isinstance(err, asyncio.TimeoutError):
                    self.stats.timeouts += 1
                if reg.name == "alive":
                    self.stats.alive_failures += 1
                self.stats.last_error = str(err)
                self._disconnect_locked()
                raise WebastoModbusError(f"Failed to write {reg.name}: {err}") from err

    # -- internals -----------------------------------------------------------
    async def _read_block(self, reg_type: RegType, address: int, count: int) -> list[int]:
        async with self._lock:
            try:
                await self._ensure_connected_locked()
                method = (
                    self._client.read_input_registers
                    if reg_type == RegType.INPUT
                    else self._client.read_holding_registers
                )
                t0 = monotonic()
                resp = await self._request(method, address=address, count=count)
                self._raise_for_error(resp, f"read {reg_type.value}@{address}")
                self.stats.record_response(monotonic() - t0)
                self._mark_ok()
                return list(resp.registers)
            except (ModbusException, OSError, asyncio.TimeoutError, WebastoModbusError) as err:
                self.stats.read_failures += 1
                if isinstance(err, asyncio.TimeoutError):
                    self.stats.timeouts += 1
                self.stats.last_error = str(err)
                self._disconnect_locked()
                raise WebastoModbusError(
                    f"Failed to read {reg_type.value} block @{address} (count={count}): {err}"
                ) from err

    async def _ensure_connected_locked(self) -> None:
        if self.connected:
            return
        client = AsyncModbusTcpClient(self.host, port=self.port, timeout=self.timeout_s, retries=0)
        ok = await client.connect()
        if not ok or not client.connected:
            self.stats.connected = False
            raise WebastoModbusError(f"Could not connect to {self.host}:{self.port}")
        if self._client is not None:
            self.stats.reconnects += 1
        self._client = client
        self.stats.connected = True
        self._new_connection = True  # coordinator re-runs the ownership handshake

    def _disconnect_locked(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._client = None
        self.stats.connected = False

    async def _request(self, method: Callable, **kwargs: Any) -> Any:
        """Call a pymodbus method, resolving the unit-id kwarg name once."""
        if self._unit_kwarg is not None:
            return await method(**kwargs, **{self._unit_kwarg: self.unit_id})
        for candidate in ("slave", "device_id", "unit"):
            try:
                resp = await method(**kwargs, **{candidate: self.unit_id})
            except TypeError as err:
                if "unexpected keyword argument" in str(err) and candidate in str(err):
                    continue
                raise
            else:
                self._unit_kwarg = candidate
                return resp
        raise WebastoModbusError("No supported pymodbus unit-id parameter found")

    @staticmethod
    def _raise_for_error(resp: Any, what: str) -> None:
        if resp is None or isinstance(resp, ExceptionResponse) or resp.isError():
            raise WebastoModbusError(f"Modbus error during {what}: {resp}")

    def _mark_ok(self) -> None:
        self.stats.connected = True
        self.stats.last_ok = monotonic()
        self.stats.last_error = None
