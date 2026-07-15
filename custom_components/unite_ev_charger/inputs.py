"""Read external Home Assistant sensors and normalise them.

Thin HA-aware wrapper around the pure parsers in units.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

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


def read_current_a(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    max_age_s: float | None = None,
    not_before: datetime | None = None,
) -> float | None:
    """Read current, optionally requiring a recent post-startup sample."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if max_age_s is not None or not_before is not None:
        updated = getattr(state, "last_updated", None)
        if updated is None:
            return None
        if not_before is not None and updated < not_before:
            return None
        if max_age_s is not None:
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            if age < 0 or age > max_age_s:
                return None
    return parse_current_a(state.state, state.attributes.get(ATTR_UNIT))
