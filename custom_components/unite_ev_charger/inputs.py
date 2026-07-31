"""Read external Home Assistant sensors and normalise them.

Thin HA-aware wrapper around the pure parsers in units.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from typing import Any

from homeassistant.core import HomeAssistant

from .units import parse_current_a, parse_power_w

ATTR_UNIT = "unit_of_measurement"


def _age_s(state: Any) -> float | None:
    """Seconds since the state was last reported. None if unknowable.

    Prefers ``last_reported`` (bumped even when the value is unchanged) so a
    steady-but-alive sensor is not mistaken for a dead one.
    """
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if stamp is None:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def read_power_w(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return parse_power_w(state.state, state.attributes.get(ATTR_UNIT))


def read_current_a(
    hass: HomeAssistant, entity_id: str | None, *, max_age_s: float | None = None
) -> float | None:
    """Read a current sensor. With ``max_age_s``, a stale reading returns None
    so the caller can fail closed instead of trusting old data."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if max_age_s is not None:
        age = _age_s(state)
        if age is None or age < 0 or age > max_age_s:
            return None
    return parse_current_a(state.state, state.attributes.get(ATTR_UNIT))
