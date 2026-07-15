"""Safety regression tests for dynamic load balancing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace

from uec.controller import ChargeControl
from uec.models import WallboxData
from uec.modbus import WebastoModbusError


class FakeStates:
    def __init__(self) -> None:
        self.values: dict[str, SimpleNamespace] = {}

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self.values.get(entity_id)

    def set(self, entity_id: str, value: float, updated: datetime) -> None:
        self.values[entity_id] = SimpleNamespace(
            state=str(value),
            attributes={"unit_of_measurement": "A"},
            last_updated=updated,
        )


class FakeHass:
    def __init__(self) -> None:
        self.states = FakeStates()


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    async def write_register(self, reg, value) -> None:
        self.writes.append((reg.name, value))


class FakeCoordinator:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.device = SimpleNamespace(max_current_a=16, min_current_a=6, phases_supported=3)
        self.data = None
        self.ownership_active = True

    async def async_write_owned(self, register, value: int) -> None:
        if not self.ownership_active:
            raise WebastoModbusError(
                "EMS ownership is not active; charger write rejected"
            )
        await self.client.write_register(register, value)

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


OPTIONS = {
    "default_mode": "fast",
    "min_current": 6,
    "max_current": 16,
    "phase_switching": False,
    "dlb_enabled": True,
    "dlb_phases": 3,
    "dlb_sensor_max_age": 30,
    "dlb_current_l1": "sensor.grid_l1",
    "dlb_current_l2": "sensor.grid_l2",
    "dlb_current_l3": "sensor.grid_l3",
    "main_fuse_a": 25,
    "dlb_margin_a": 3,
    "failsafe_current": 6,
    "increase_delay": 10,
    "increase_step": 1,
}


def _control(**options):
    hass = FakeHass()
    client = FakeClient()
    coordinator = FakeCoordinator(client)
    merged = {**OPTIONS, **options}
    control = ChargeControl(hass, SimpleNamespace(options=merged), coordinator)
    return control, hass, client


def _data(set_current: int = 16) -> WallboxData:
    data = WallboxData()
    data.charge_point_state_raw = 2
    data.cable_state_raw = 2
    data.phase_switch_raw = 1
    data.current_l1_a = 6.0
    data.current_l2_a = 6.0
    data.current_l3_a = 6.0
    data.set_current_a = set_current
    return data


def _set_grid(hass: FakeHass, values: tuple[float, float, float], updated: datetime) -> None:
    for phase, value in enumerate(values, start=1):
        hass.states.set(f"sensor.grid_l{phase}", value, updated)


def test_missing_required_phase_applies_failsafe_and_withholds_heartbeat():
    control, hass, client = _control()
    now = datetime.now(timezone.utc)
    hass.states.set("sensor.grid_l1", 10, now)
    hass.states.set("sensor.grid_l3", 10, now)

    asyncio.run(control.async_apply(_data()))

    assert client.writes == [("set_current_a", 6)]
    assert control.computed_setpoint == 6
    assert control.dlb_healthy is False
    assert "L2" in control.dlb_failure_reason
    assert control.heartbeat_allowed is False


def test_stale_phase_applies_failsafe():
    control, hass, client = _control()
    control._started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    stale = datetime.now(timezone.utc) - timedelta(seconds=31)
    _set_grid(hass, (10, 10, 10), stale)

    asyncio.run(control.async_apply(_data()))

    assert client.writes == [("set_current_a", 6)]
    assert control.dlb_healthy is False
    assert control.heartbeat_allowed is False


def test_signed_three_phase_snapshot_preserves_export():
    control, hass, _ = _control()
    _set_grid(hass, (20, -2, 19), datetime.now(timezone.utc))

    cap, error = control._dlb_cap(_data(set_current=6))

    assert error is None
    assert cap == 8


def test_grid_stop_overrides_solar_anti_cycle_and_phase_quiet_period():
    control, hass, client = _control(default_mode="solar", meter_model="none")
    control.mode = "solar"
    control._charge_started = monotonic()
    control._last_switch = monotonic()
    _set_grid(hass, (23, -3, 20), datetime.now(timezone.utc))

    asyncio.run(control.async_apply(_data(set_current=6)))

    assert control.computed_setpoint == 0
    assert client.writes == [("set_current_a", 0)]
    assert control.heartbeat_allowed is True


def test_increases_wait_and_step_while_reductions_are_immediate():
    control, _, _ = _control()
    limits = control.limits

    assert control._limit_increase(16, 6, limits) == 6
    control._increase_since = monotonic() - 11
    assert control._limit_increase(16, 6, limits) == 7
    assert control._limit_increase(16, 7, limits) == 7
    control._increase_since = monotonic() - 11
    assert control._limit_increase(16, 7, limits) == 8
    assert control._limit_increase(5, 8, limits) == 5


def test_implausible_grid_current_fails_closed():
    control, hass, _ = _control()
    _set_grid(hass, (10, 1000, 10), datetime.now(timezone.utc))

    cap, error = control._dlb_cap(_data())

    assert cap is None
    assert "implausible" in error


def test_dlb_failure_and_recovery_logs_are_transition_only(caplog):
    control, hass, _ = _control()
    now = datetime.now(timezone.utc)
    hass.states.set("sensor.grid_l1", 10, now)
    hass.states.set("sensor.grid_l3", 10, now)

    with caplog.at_level(logging.INFO, logger="uec.controller"):
        asyncio.run(control.async_apply(_data()))
        asyncio.run(control.async_apply(_data()))
        _set_grid(hass, (10, 10, 10), datetime.now(timezone.utc))
        asyncio.run(control.async_apply(_data(set_current=6)))

    assert caplog.text.count("DLB paused:") == 1
    assert "applying 6 A failsafe and withholding Alive" in caplog.text
    assert (
        "DLB input recovered; normal control and Alive heartbeat resumed"
        in caplog.text
    )


def test_dlb_calculation_logs_inputs_and_cap_at_debug(caplog):
    control, hass, _ = _control()
    _set_grid(hass, (20, -2, 19), datetime.now(timezone.utc))

    with caplog.at_level(logging.DEBUG, logger="uec.controller"):
        cap, error = control._dlb_cap(_data(set_current=6))

    assert error is None
    assert cap == 8
    assert "grid=[20.0, -2.0, 19.0] A" in caplog.text
    assert "charger=[6.0, 6.0, 6.0] A" in caplog.text
    assert "fuse=25 A margin=3 A cap=8.00 A" in caplog.text
