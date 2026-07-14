"""Failsafe and heartbeat handling.

The wallbox falls back to ``failsafe_current`` if it does not receive an alive
write (register 6000) within ``failsafe_timeout`` seconds. We program a safe
failsafe at startup and write the heartbeat every poll cycle. This is what makes
it acceptable to steer the charging current from Home Assistant: if HA, the
network, or this integration dies, the charger stops (or limits) on its own.
"""
from __future__ import annotations

import logging

from . import registers as R
from .const import (
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    HEARTBEAT_ALIVE_VALUE,
)
from .modbus import WebastoModbus, WebastoModbusError

_LOGGER = logging.getLogger(__name__)


async def program_failsafe(
    client: WebastoModbus,
    *,
    failsafe_current_a: int = DEFAULT_FAILSAFE_CURRENT_A,
    failsafe_timeout_s: int = DEFAULT_FAILSAFE_TIMEOUT_S,
    required: bool = False,
) -> bool:
    """Configure the charger's failsafe current and timeout.

    Current is written before enabling/changing the timeout so a partial
    handshake can never arm a watchdog with an unknown fallback current.
    DLB callers set ``required`` because continuing to send heartbeats without
    a confirmed failsafe would defeat that safety contract. Other modes retain
    compatibility with firmware that does not implement these registers.
    """
    try:
        await client.write_register(R.FAILSAFE_CURRENT_A, failsafe_current_a)
        await client.write_register(R.FAILSAFE_TIMEOUT_S, failsafe_timeout_s)
        _LOGGER.debug(
            "Programmed failsafe: %s A after %s s timeout",
            failsafe_current_a,
            failsafe_timeout_s,
        )
        return True
    except WebastoModbusError as err:
        _LOGGER.warning("Could not program failsafe registers: %s", err)
        if required:
            raise
        return False


async def write_heartbeat(client: WebastoModbus) -> None:
    """Write the alive register. Raises so callers can surface comms loss."""
    await client.write_register(R.ALIVE, HEARTBEAT_ALIVE_VALUE)
