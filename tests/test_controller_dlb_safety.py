"""DLB must fail CLOSED: untrustworthy grid data is not evidence of headroom.

Drives ChargeControl._dlb_cap directly with a fake hass.states, so we exercise
the real rule (staleness, plausibility, partial data) without Home Assistant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from uec.controller import ChargeControl
from uec.models import WallboxData

OPTIONS = {
    "dlb_enabled": True,
    "main_fuse_a": 25,
    "dlb_margin_a": 2,
    "dlb_current_l1": "sensor.l1",
    "dlb_current_l2": "sensor.l2",
    "dlb_current_l3": "sensor.l3",
}


class FakeStates:
    def __init__(self, values: dict[str, object], age_s: float = 1.0) -> None:
        self._values = values
        self._age_s = age_s

    def get(self, entity_id: str):
        if entity_id not in self._values:
            return None
        return SimpleNamespace(
            state=self._values[entity_id],
            attributes={"unit_of_measurement": "A"},
            last_reported=datetime.now(timezone.utc) - timedelta(seconds=self._age_s),
            last_updated=datetime.now(timezone.utc) - timedelta(seconds=self._age_s),
        )


def _control(values, *, age_s: float = 1.0, options=None):
    hass = SimpleNamespace(states=FakeStates(values, age_s))
    coordinator = SimpleNamespace(
        client=None,
        device=SimpleNamespace(phases_supported=3),
        data=None,
        async_update_listeners=lambda: None,
    )
    return ChargeControl(hass, SimpleNamespace(options=options or OPTIONS), coordinator)


def _data() -> WallboxData:
    d = WallboxData()
    d.current_l1_a = d.current_l2_a = d.current_l3_a = 6.0
    return d


def test_healthy_sensors_produce_a_cap():
    ctl = _control({"sensor.l1": "18", "sensor.l2": "10", "sensor.l3": "10"})
    # L1 room = 25 - 2 - (18 - 6) = 11 -> lowest across phases
    assert ctl._dlb_cap(_data()) == 11.0


def test_missing_sensor_fails_closed():
    # L3 configured but absent from the state machine -> pause, not "no cap"
    ctl = _control({"sensor.l1": "18", "sensor.l2": "10"})
    assert ctl._dlb_cap(_data()) == 0.0


def test_stale_sensor_fails_closed():
    ctl = _control({"sensor.l1": "18", "sensor.l2": "10", "sensor.l3": "10"}, age_s=9999)
    assert ctl._dlb_cap(_data()) == 0.0


def test_unavailable_sensor_fails_closed():
    ctl = _control({"sensor.l1": "unavailable", "sensor.l2": "10", "sensor.l3": "10"})
    assert ctl._dlb_cap(_data()) == 0.0


def test_implausible_sensor_fails_closed():
    # 99999 A is not headroom, it is a broken sensor
    ctl = _control({"sensor.l1": "99999", "sensor.l2": "10", "sensor.l3": "10"})
    assert ctl._dlb_cap(_data()) == 0.0


def test_no_sensor_configured_fails_closed():
    opts = {**OPTIONS, "dlb_current_l1": None, "dlb_current_l2": None, "dlb_current_l3": None}
    ctl = _control({}, options=opts)
    assert ctl._dlb_cap(_data()) == 0.0


def test_dlb_disabled_means_no_cap_at_all():
    # Not the same as failing closed: DLB off must not pause charging.
    opts = {**OPTIONS, "dlb_enabled": False}
    ctl = _control({}, options=opts)
    assert ctl._dlb_cap(_data()) is None
