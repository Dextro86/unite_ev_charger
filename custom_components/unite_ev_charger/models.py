"""Data models for the Webasto Unite integration."""
from __future__ import annotations

from dataclasses import dataclass

from . import registers as R
from .registers import ChargePointState


@dataclass(slots=True)
class DeviceInfo:
    """Static identity, read once at setup."""

    serial_number: str | None = None
    brand: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    phases_supported: int | None = None
    min_current_a: int = 6  # refined from the wallbox at setup when available
    max_current_a: int = 32


@dataclass(slots=True)
class WallboxData:
    """One polling snapshot decoded from the telemetry + session blocks."""

    charge_point_state_raw: int = 0
    charging_state_raw: int = 0
    cable_state_raw: int = 0

    current_l1_a: float = 0.0
    current_l2_a: float = 0.0
    current_l3_a: float = 0.0
    voltage_l1_v: float = 0.0
    voltage_l2_v: float = 0.0
    voltage_l3_v: float = 0.0
    active_power_w: float = 0.0
    meter_energy_kwh: float = 0.0

    session_energy_kwh: float = 0.0
    session_duration_s: int = 0

    # Control snapshot (read from holding registers).
    set_current_a: int | None = None
    phase_switch_raw: int | None = None
    # Raw phase-capability register 404 (0 = 1-phase, 1 = 3-phase). Read every
    # cycle, not just at setup: a Unite can report it wrong while still booting.
    phase_capability_raw: int | None = None

    # -- derived -----------------------------------------------------------
    @property
    def state(self) -> ChargePointState | None:
        try:
            return ChargePointState(self.charge_point_state_raw)
        except ValueError:
            return None

    @property
    def vehicle_connected(self) -> bool:
        return self.cable_state_raw >= 2

    @property
    def charging(self) -> bool:
        return self.charge_point_state_raw == ChargePointState.CHARGING

    @property
    def faulted(self) -> bool:
        return self.charge_point_state_raw == ChargePointState.FAULTED

    @property
    def iec61851_status(self) -> str:
        """IEC 61851 status letter, as expected by evcc's HA charger."""
        if self.faulted:
            return "F"
        if not self.vehicle_connected:
            return "A"
        if self.charging:
            return "C"
        return "B"

    @property
    def actual_current_a(self) -> float:
        return max(self.current_l1_a, self.current_l2_a, self.current_l3_a)

    @property
    def phases_in_use(self) -> int:
        return sum(1 for c in (self.current_l1_a, self.current_l2_a, self.current_l3_a) if c > 0.2)


def parse_telemetry(block: list[int]) -> WallboxData:
    """Decode the 1000..1037 telemetry block into a WallboxData snapshot."""
    base = R.TELEMETRY_BASE
    data = WallboxData()
    data.charge_point_state_raw = R.block_u16(block, base, R.CHARGE_POINT_STATE)
    data.charging_state_raw = R.block_u16(block, base, R.CHARGING_STATE)
    data.cable_state_raw = R.block_u16(block, base, R.CABLE_STATE)
    data.current_l1_a = R.block_u16(block, base, R.CURRENT_L1_A) * R.CURRENT_L1_A.scale
    data.current_l2_a = R.block_u16(block, base, R.CURRENT_L2_A) * R.CURRENT_L2_A.scale
    data.current_l3_a = R.block_u16(block, base, R.CURRENT_L3_A) * R.CURRENT_L3_A.scale
    data.voltage_l1_v = R.block_u16(block, base, R.VOLTAGE_L1_V)
    data.voltage_l2_v = R.block_u16(block, base, R.VOLTAGE_L2_V)
    data.voltage_l3_v = R.block_u16(block, base, R.VOLTAGE_L3_V)
    data.active_power_w = R.block_u32(block, base, R.ACTIVE_POWER_W)
    data.meter_energy_kwh = R.block_u32(block, base, R.METER_ENERGY_KWH) * R.METER_ENERGY_KWH.scale
    return data


def apply_session(data: WallboxData, block: list[int]) -> None:
    """Decode the 1502..1509 session block into an existing snapshot."""
    base = R.SESSION_BASE
    data.session_energy_kwh = R.block_u32(block, base, R.SESSION_ENERGY_KWH) * R.SESSION_ENERGY_KWH.scale
    data.session_duration_s = R.block_u32(block, base, R.SESSION_DURATION_S)
