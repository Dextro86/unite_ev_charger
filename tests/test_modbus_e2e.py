"""End-to-end tests: the real Modbus client against the behavioral simulator.

These exercise the whole hardware-facing layer over a real TCP round-trip -
client framing, block reads, decoding, the write path and the failsafe
watchdog - without Home Assistant and without the physical wallbox.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import unite_simulator as sim  # noqa: E402

from uec import registers as R  # noqa: E402
from uec.models import apply_session, parse_telemetry  # noqa: E402
from uec.modbus import WebastoModbus  # noqa: E402
from uec.registers import ChargePointState  # noqa: E402


async def _serve(state: sim.UniteState, scenario: sim.Scenario):
    server, _ = await sim.start(state, scenario, "127.0.0.1", 0, with_tick=False)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _run(coro):
    return asyncio.run(coro)


def test_identity_read():
    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            serial = await client.read_register(R.SERIAL_NUMBER)
            await client.async_close()
            return serial
        finally:
            server.close()
            await server.wait_closed()

    assert _run(scenario()) == "SIM-UNITE-0001"


def test_charging_telemetry_roundtrip():
    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        scn.car_max_current = 16
        scn.car_max_phases = 3
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.SET_CURRENT_A, 16)
            state.tick(scn)
            block = await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT)
            await client.async_close()
            return parse_telemetry(block)
        finally:
            server.close()
            await server.wait_closed()

    data = _run(scenario())
    assert data.state == ChargePointState.CHARGING
    assert data.current_l1_a == 16.0
    assert data.phases_in_use == 3
    assert data.active_power_w == 16 * 3 * 230


def test_phase_mismatch_is_measured_correctly():
    """Register says 3 phases, the car only draws 1 -> we must see 1."""

    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        scn.phase_mismatch = True  # reg 405 = 3p, car draws 1p
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.SET_CURRENT_A, 16)
            state.tick(scn)
            block = await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT)
            await client.async_close()
            return parse_telemetry(block)
        finally:
            server.close()
            await server.wait_closed()

    data = _run(scenario())
    assert data.phases_in_use == 1
    assert data.current_l1_a == 16.0
    assert data.current_l2_a == 0.0
    assert data.current_l3_a == 0.0


def test_failsafe_falls_back_when_heartbeat_stale():
    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.SET_CURRENT_A, 16)
            # Pretend no heartbeat for longer than the failsafe timeout (30 s).
            state.last_heartbeat = time.monotonic() - 40
            state.tick(scn)
            block = await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT)
            await client.async_close()
            return parse_telemetry(block)
        finally:
            server.close()
            await server.wait_closed()

    data = _run(scenario())
    # Falls back to the simulator's failsafe current (6 A), not the set 16 A.
    assert data.current_l1_a == 6.0


def test_write_setpoint_is_persisted():
    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.SET_CURRENT_A, 10)
            value = await client.read_register(R.SET_CURRENT_A)
            await client.async_close()
            return value
        finally:
            server.close()
            await server.wait_closed()

    assert _run(scenario()) == 10


def test_reconnects_after_disconnect():
    """After the connection drops, the next operation reconnects by itself."""

    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.read_register(R.SERIAL_NUMBER)
            assert client.connected
            await client.async_close()  # simulate a dropped connection
            assert not client.connected
            serial = await client.read_register(R.SERIAL_NUMBER)  # auto-reconnect
            connected = client.connected
            await client.async_close()
            return serial, connected
        finally:
            server.close()
            await server.wait_closed()

    serial, connected = _run(scenario())
    assert serial == "SIM-UNITE-0001"
    assert connected is True


def test_failsafe_recovers_when_heartbeat_resumes():
    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.SET_CURRENT_A, 16)
            state.last_heartbeat = time.monotonic() - 40  # stale -> failsafe
            state.tick(scn)
            stale = parse_telemetry(await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT))
            await client.write_register(R.ALIVE, 1)  # heartbeat resumes
            state.tick(scn)
            recovered = parse_telemetry(await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT))
            await client.async_close()
            return stale.current_l1_a, recovered.current_l1_a
        finally:
            server.close()
            await server.wait_closed()

    stale, recovered = _run(scenario())
    assert stale == 6.0       # fell back to failsafe current
    assert recovered == 16.0  # back to the commanded current


def test_take_new_connection_flags_first_connect_and_reconnect():
    """The ownership-handshake flag fires on first connect and after a reconnect."""

    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            before = client.take_new_connection()        # no connection yet
            await client.read_register(R.SERIAL_NUMBER)   # first connect
            first = client.take_new_connection()          # -> True (and clears)
            again = client.take_new_connection()          # -> already cleared
            await client.async_close()                    # drop the connection
            await client.read_register(R.SERIAL_NUMBER)   # auto-reconnect
            after_reconnect = client.take_new_connection()
            await client.async_close()
            return before, first, again, after_reconnect
        finally:
            server.close()
            await server.wait_closed()

    before, first, again, after_reconnect = _run(scenario())
    assert before is False
    assert first is True
    assert again is False
    assert after_reconnect is True


def test_evcc_style_current_and_phase_control():
    """The external (evcc) path: write phase + current, see it reflected."""

    async def scenario():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        scn.car_max_current = 16
        scn.car_max_phases = 3
        server, port = await _serve(state, scn)
        try:
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            await client.write_register(R.PHASE_SWITCH, 1)  # 3-phase
            await client.write_register(R.SET_CURRENT_A, 10)
            state.tick(scn)
            three = parse_telemetry(await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT))
            await client.write_register(R.PHASE_SWITCH, 0)  # 1-phase
            state.tick(scn)
            one = parse_telemetry(await client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT))
            await client.async_close()
            return three, one
        finally:
            server.close()
            await server.wait_closed()

    three, one = _run(scenario())
    assert three.phases_in_use == 3
    assert three.current_l1_a == 10.0
    assert one.phases_in_use == 1
