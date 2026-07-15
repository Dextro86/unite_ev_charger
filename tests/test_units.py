"""Unit tests for sensor unit normalisation (the energy-vs-power guard)."""
from __future__ import annotations

from uec import units


def test_power_watts_passthrough():
    assert units.parse_power_w("3680", "W") == 3680.0
    assert units.parse_power_w(3680, None) == 3680.0  # no unit -> assume W


def test_power_kilowatts_scaled():
    assert units.parse_power_w("3.68", "kW") == 3680.0
    assert units.parse_power_w(0.5, "MW") == 500000.0


def test_power_rejects_energy_unit():
    # the classic crash: an energy sensor pointed at a power input
    assert units.parse_power_w("12.5", "kWh") is None
    assert units.parse_power_w("100", "Wh") is None


def test_power_rejects_unavailable_and_garbage():
    assert units.parse_power_w("unavailable", "W") is None
    assert units.parse_power_w("unknown", "W") is None
    assert units.parse_power_w(None, "W") is None
    assert units.parse_power_w("n/a", "W") is None
    assert units.parse_power_w("55", "%") is None


def test_current_normalisation():
    assert units.parse_current_a("16", "A") == 16.0
    assert units.parse_current_a("16000", "mA") == 16.0
    assert units.parse_current_a("unavailable", "A") is None
    assert units.parse_current_a("16", "W") is None  # wrong quantity


def test_non_finite_numbers_are_rejected():
    assert units.parse_current_a("nan", "A") is None
    assert units.parse_current_a("inf", "A") is None
    assert units.parse_power_w("-inf", "W") is None
