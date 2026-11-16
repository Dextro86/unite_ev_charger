"""Tests for the opt-in evcc watchdog in external control mode.

Same contract as the bridge: cap at the failsafe current while evcc itself is
silent, keep evcc's intent, restore it when evcc is back. Extra rule here:
internal modes never arm the watchdog, because there Home Assistant itself is
the controller and a silent evcc means nothing.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import unite_simulator as sim  # noqa: E402

from uec import registers as R  # noqa: E402
from uec.controller import ChargeControl  # noqa: E402
from uec.coordinator import WebastoCoordinator  # noqa: E402
from uec.evcc_watchdog import (  # noqa: E402
    TRANSITION_LOST,
    TRANSITION_RECOVERED,
    EvccWatchdog,
)
from uec.modbus import WebastoModbus  # noqa: E402
from uec.models import WallboxData  # noqa: E402

EXT_OPTIONS = {
    "control_mode": "external",
    "min_current": 6,
    "max_current": 16,
    "failsafe_current": 6,
    "failsafe_timeout": 30,
    "phase_recovery_enabled": False,
    "phase_recovery_observe": 0,
    "phase_recovery_dwell": 0,
    "poll_interval": 10,
    "evcc_watchdog_mode": "api",
    "evcc_api_url": "http://192.0.2.20:7070",
    "evcc_mqtt_topic": "evcc/updated",
    "evcc_watchdog_timeout": 120,
}
INT_OPTIONS = {**EXT_OPTIONS, "control_mode": "internal"}


class FakeHass:
    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


class FakeClient:
    """Controller-level fake: only writes matter here."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    async def write_register(self, reg, value) -> None:
        self.writes.append((reg.name, value))


def _coordinator(options: dict, client) -> WebastoCoordinator:
    hass = FakeHass()
    entry = SimpleNamespace(entry_id="test", options=dict(options), data={"host": "192.0.2.1"})
    coord = WebastoCoordinator(hass, entry, client)
    coord.controller = ChargeControl(hass, entry, coord)
    return coord


def _watchdog(**overrides):
    now = [1000.0]
    kwargs = {"mode": "api", "timeout_s": 120, "api_url": "http://192.0.2.20:7070"}
    kwargs.update(overrides)
    return EvccWatchdog(FakeHass(), clock=lambda: now[0], **kwargs), now  # type: ignore[arg-type]


# --- coordinator seam ----------------------------------------------------------


def test_cap_follows_throttle_and_failsafe_setting() -> None:
    coord = _coordinator(EXT_OPTIONS, FakeClient())
    assert coord.cap_external_current(16) == 16  # no watchdog: untouched
    coord._evcc_throttled = True
    assert coord.cap_external_current(16) == 6
    assert coord.cap_external_current(0) == 0
    assert coord.cap_external_current(3) == 3  # below the cap stays as-is


def test_watchdog_only_arms_in_external_mode() -> None:
    async def main():
        internal = _coordinator(INT_OPTIONS, FakeClient())
        await internal.async_start_evcc_watchdog(session=None)
        assert internal.evcc_watchdog is None

        off = _coordinator({**EXT_OPTIONS, "evcc_watchdog_mode": "off"}, FakeClient())
        await off.async_start_evcc_watchdog(session=None)
        assert off.evcc_watchdog is None

        external = _coordinator(EXT_OPTIONS, FakeClient())
        await external.async_start_evcc_watchdog(session=None)
        assert external.evcc_watchdog is not None
        await external.async_stop_evcc_watchdog()
        assert external.evcc_watchdog is None

    asyncio.run(main())


def test_evcc_command_counts_as_sign_of_life() -> None:
    async def main():
        coord = _coordinator(EXT_OPTIONS, FakeClient())
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0
        assert watchdog.is_stale()
        await coord.controller.async_external_set_current(10)
        return watchdog.is_stale()

    assert asyncio.run(main()) is False


# --- transitions through the real controller -----------------------------------


def test_lost_caps_and_recovered_restores() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(EXT_OPTIONS, client)
        coord.controller._current_intent = 16
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0  # evcc goes quiet
        assert coord._evaluate_evcc_watchdog() == TRANSITION_LOST
        await coord._async_apply_evcc_transition(TRANSITION_LOST)
        assert coord.evcc_throttled
        assert ("set_current_a", 6) in client.writes
        assert coord.controller._current_intent == 16  # intent kept
        watchdog.mark_alive()  # evcc answers again
        assert coord._evaluate_evcc_watchdog() == TRANSITION_RECOVERED
        await coord._async_apply_evcc_transition(TRANSITION_RECOVERED)
        assert not coord.evcc_throttled
        return client.writes

    writes = asyncio.run(main())
    assert ("set_current_a", 16) in writes  # intent put back, not waited for


def test_lost_during_recovery_pause_keeps_zero() -> None:
    """A LOST transition must never break the recovery pause with a cap > 0."""

    async def main():
        client = FakeClient()
        coord = _coordinator(EXT_OPTIONS, client)
        coord.controller._current_intent = 16
        coord.controller._buffer_commands = True  # dwell owns 0 A
        watchdog, now = _watchdog()
        coord.evcc_watchdog = watchdog
        now[0] += 500.0
        assert coord._evaluate_evcc_watchdog() == TRANSITION_LOST
        await coord._async_apply_evcc_transition(TRANSITION_LOST)
        return client.writes

    writes = asyncio.run(main())
    assert writes, "the pause still owns the setpoint"
    assert all(value == 0 for _, value in writes)


def test_external_set_current_capped_but_intent_kept() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(EXT_OPTIONS, client)
        coord._evcc_throttled = True
        await coord.controller.async_external_set_current(16)
        return client.writes, coord.controller._current_intent

    writes, intent = asyncio.run(main())
    assert ("set_current_a", 6) in writes
    assert intent == 16  # the entity keeps showing evcc's wish


# --- full poll against the simulator -------------------------------------------


def _sim_entry():
    return SimpleNamespace(entry_id="test", options=dict(EXT_OPTIONS), data={"host": "127.0.0.1"})


def test_full_poll_caps_every_write_while_evcc_is_silent() -> None:
    async def main():
        state, scn = sim.UniteState(), sim.Scenario()
        scn.plugged = True
        scn.car_max_current = 16
        scn.car_max_phases = 3
        server, _ = await sim.start(state, scn, "127.0.0.1", 0, with_tick=False)
        port = server.sockets[0].getsockname()[1]
        try:
            state.tick(scn)  # let the simulator settle the plugged-in state
            hass = FakeHass()
            client = WebastoModbus("127.0.0.1", port, unit_id=255)
            writes: list[tuple[str, int]] = []
            real_write = client.write_register

            async def recording_write(reg, value):
                writes.append((reg.name, value))
                return await real_write(reg, value)

            client.write_register = recording_write  # type: ignore[method-assign]
            coord = WebastoCoordinator(hass, _sim_entry(), client)
            coord.controller = ChargeControl(hass, _sim_entry(), coord)
            watchdog, now = _watchdog()
            coord.evcc_watchdog = watchdog
            coord.controller._current_intent = 16  # evcc's last wish
            now[0] += 500.0  # evcc goes quiet
            data: WallboxData = await coord._async_update_data()
            assert data.vehicle_connected
            assert coord.evcc_throttled
            currents = [v for (k, v) in writes if k == "set_current_a"]
            assert currents, "a fresh connection must program a current"
            assert all(v <= 6 for v in currents), f"every write capped: {currents}"
            # evcc answers again: the next poll restores its wish.
            watchdog.mark_alive()
            state.tick(scn)
            second: WallboxData = await coord._async_update_data()
            assert second.vehicle_connected
            assert not coord.evcc_throttled
            await client.async_close()
            return [v for (k, v) in writes if k == "set_current_a"]
        finally:
            server.close()
            await server.wait_closed()

    currents = asyncio.run(main())
    assert 16 in currents, f"evcc's wish restored on recovery: {currents}"


def test_reconnect_reassert_is_capped() -> None:
    """A Modbus reconnect while evcc is lost must reassert the cap."""

    async def main():
        client = FakeClient()
        coord = _coordinator(EXT_OPTIONS, client)
        coord._evcc_throttled = True
        coord.controller._current_intent = 16
        await coord.controller.async_on_reconnect(phase_raw=1)
        return client.writes

    writes = asyncio.run(main())
    assert ("set_current_a", 6) in writes
    assert ("set_current_a", 16) not in writes


def test_session_start_reassert_is_capped() -> None:
    """Plugging in while evcc is lost must reassert the cap, never the wish."""

    async def main():
        client = FakeClient()
        coord = _coordinator(EXT_OPTIONS, client)
        coord._evcc_throttled = True
        coord.controller._current_intent = 16
        data = WallboxData()
        data.charge_point_state_raw = 2
        data.cable_state_raw = 2  # vehicle just connected
        await coord.controller.async_apply(data)
        return client.writes

    writes = asyncio.run(main())
    assert writes == [("set_current_a", 6)]
