"""Diagnostics for the Unite EV Charger."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from time import monotonic
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import control as ctrl
from .const import (
    CONF_BASELINE_REQUIRED,
    CONF_HOST,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DOMAIN,
)
from .coordinator import WebastoCoordinator

# Never leak the wallbox address, serial, or the web-UI credentials in a
# downloadable diagnostics file.
TO_REDACT = {CONF_HOST, CONF_REST_USERNAME, CONF_REST_PASSWORD, "serial_number"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    controller = coordinator.controller
    data = coordinator.data
    original = coordinator.original_configuration

    out: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "device": async_redact_data(asdict(coordinator.device), TO_REDACT),
        "modbus_stats": asdict(coordinator.client.stats),
        "telemetry_register_type": coordinator.telemetry_register_type,
        "failsafe_configured": coordinator.failsafe_configured,
        "ownership": {
            "state": coordinator.ownership_state.value,
            "requested": coordinator.automatic_control_requested,
            "dirty": coordinator.ownership_dirty,
            "baseline_required": bool(entry.data.get(CONF_BASELINE_REQUIRED)),
            "original": asdict(original) if original is not None else None,
        },
        "wallbox": asdict(data) if data is not None else None,
        "last_update_success": coordinator.last_update_success,
        "rest": {
            "last_restart_at": _iso(coordinator.rest_last_restart_at),
            "last_restart_result": coordinator.rest_last_restart_result,
        },
    }

    if controller is not None:
        out["controller"] = {
            "mode": controller.mode,
            "charging_enabled": controller.charging_enabled,
            "manual_current": controller.manual_current,
            "computed_setpoint": controller.computed_setpoint,
            "available_surplus_w": controller.available_surplus_w,
            "dlb_healthy": controller.dlb_healthy,
            "dlb_failure_reason": controller.dlb_failure_reason,
            "heartbeat_allowed": controller.heartbeat_allowed,
            "recovery_status": controller.recovery_status,
            "recovery_active": controller.recovery_active,
            "recovery_remaining_s": controller.recovery_remaining_s,
            "last_recovery_at": _iso(controller.last_recovery_at),
            "last_recovery_result": controller.last_recovery_result,
        }

    # Interpreted state (the State Inspector), so a bug report reads on its own.
    if data is not None:
        mismatch = ctrl.is_phase_mismatch(
            data.charging, data.phase_switch_raw,
            data.current_l1_a, data.current_l2_a, data.current_l3_a,
        )
        restarting = (
            coordinator.rest_restart_until is not None
            and monotonic() < coordinator.rest_restart_until
        )
        out["interpreted"] = {
            "charger_state": ctrl.derive_charger_state(
                connection_ok=coordinator.last_update_success,
                restarting=restarting,
                faulted=data.faulted,
                vehicle_connected=data.vehicle_connected,
                charging=data.charging,
                phase_mismatch=mismatch,
                recovery_active=controller.recovery_active if controller is not None else False,
            ),
            "phase_mismatch": mismatch,
        }

    return out
