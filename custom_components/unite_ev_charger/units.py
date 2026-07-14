"""Pure unit parsing/normalisation for external HA sensors.

Kept HA-free and tested: this is exactly where the old integration crashed
(people pointing it at an energy sensor in kWh). We normalise power to Watts and
current to Amperes, and reject the wrong physical quantity instead of blowing up.
"""
from __future__ import annotations

from math import isfinite

_INVALID = {None, "", "unknown", "unavailable", "none"}
ENERGY_UNITS = {"Wh", "kWh", "MWh"}
_POWER_SCALE = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
_CURRENT_SCALE = {"A": 1.0, "mA": 0.001}


def _to_float(value: object) -> float | None:
    if isinstance(value, str) and value.strip().lower() in _INVALID:
        return None
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def parse_power_w(value: object, unit: str | None) -> float | None:
    """Normalise a power reading to Watts. Returns None if invalid or not power."""
    num = _to_float(value)
    if num is None:
        return None
    if unit in ENERGY_UNITS:
        return None  # energy sensor pointed at a power input -> reject
    if unit in _POWER_SCALE:
        return num * _POWER_SCALE[unit]
    if unit is None:
        return num  # assume Watts when no unit is exposed
    return None  # some other quantity (Hz, %, ...) -> reject


def parse_current_a(value: object, unit: str | None) -> float | None:
    """Normalise a current reading to Amperes. Returns None if invalid or not current."""
    num = _to_float(value)
    if num is None:
        return None
    if unit in _CURRENT_SCALE:
        return num * _CURRENT_SCALE[unit]
    if unit is None:
        return num  # assume Amperes when no unit is exposed
    return None
