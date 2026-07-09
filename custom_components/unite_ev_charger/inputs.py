"""Read external Home Assistant sensors and normalise them.

Thin HA-aware wrapper around the pure parsers in units.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .units import parse_current_a, parse_power_w

ATTR_UNIT = "unit_of_measurement"


def read_power_w(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return parse_power_w(state.state, state.attributes.get(ATTR_UNIT))


def read_current_a(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return parse_current_a(state.state, state.attributes.get(ATTR_UNIT))
