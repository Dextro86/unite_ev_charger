"""Verified Modbus register map for the Webasto/Ampure Unite (Vestel EVC04).

Telemetry lives on INPUT registers; control lives on HOLDING registers.
Registers that appeared in earlier home-grown maps but could not be
independently confirmed (e.g. 1002 evse_state, 1006 error_code) are
intentionally omitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RegType(str, Enum):
    INPUT = "input"
    HOLDING = "holding"


class ValType(str, Enum):
    U16 = "u16"
    S16 = "s16"
    U32 = "u32"
    STRING = "string"
    BOOL = "bool"


@dataclass(frozen=True, slots=True)
class RegisterDef:
    name: str
    address: int
    reg_type: RegType
    val_type: ValType = ValType.U16
    count: int = 1
    scale: float = 1.0
    writable: bool = False


# --- Static identity (read once) -------------------------------------------
SERIAL_NUMBER = RegisterDef("serial_number", 100, RegType.INPUT, ValType.STRING, count=25)
BRAND = RegisterDef("brand", 190, RegType.INPUT, ValType.STRING, count=10)
MODEL = RegisterDef("model", 210, RegType.INPUT, ValType.STRING, count=5)
FIRMWARE_VERSION = RegisterDef("firmware_version", 230, RegType.INPUT, ValType.STRING, count=50)
NUMBER_OF_PHASES = RegisterDef("number_of_phases", 404, RegType.INPUT, ValType.U16)

# --- Static-ish limits (read occasionally) ---------------------------------
MIN_CURRENT_HW_A = RegisterDef("min_current_hw_a", 1102, RegType.INPUT, ValType.U16)
MAX_CURRENT_CABLE_A = RegisterDef("max_current_cable_a", 1106, RegType.INPUT, ValType.U16)

# --- Telemetry block 1000..1037 (INPUT) ------------------------------------
TELEMETRY_BASE = 1000
TELEMETRY_COUNT = 38  # 1000..1037 inclusive

CHARGE_POINT_STATE = RegisterDef("charge_point_state", 1000, RegType.INPUT, ValType.U16)
CHARGING_STATE = RegisterDef("charging_state", 1001, RegType.INPUT, ValType.U16)
CABLE_STATE = RegisterDef("cable_state", 1004, RegType.INPUT, ValType.U16)
CURRENT_L1_A = RegisterDef("current_l1_a", 1008, RegType.INPUT, ValType.U16, scale=0.001)
CURRENT_L2_A = RegisterDef("current_l2_a", 1010, RegType.INPUT, ValType.U16, scale=0.001)
CURRENT_L3_A = RegisterDef("current_l3_a", 1012, RegType.INPUT, ValType.U16, scale=0.001)
VOLTAGE_L1_V = RegisterDef("voltage_l1_v", 1014, RegType.INPUT, ValType.U16)
VOLTAGE_L2_V = RegisterDef("voltage_l2_v", 1016, RegType.INPUT, ValType.U16)
VOLTAGE_L3_V = RegisterDef("voltage_l3_v", 1018, RegType.INPUT, ValType.U16)
ACTIVE_POWER_W = RegisterDef("active_power_w", 1020, RegType.INPUT, ValType.U32, count=2)
METER_ENERGY_KWH = RegisterDef("meter_energy_kwh", 1036, RegType.INPUT, ValType.U32, count=2, scale=0.1)

# --- Session block 1502..1509 (INPUT) --------------------------------------
SESSION_BASE = 1502
SESSION_COUNT = 8  # 1502..1509 inclusive

SESSION_ENERGY_KWH = RegisterDef("session_energy_kwh", 1502, RegType.INPUT, ValType.U32, count=2, scale=0.001)
SESSION_DURATION_S = RegisterDef("session_duration_s", 1508, RegType.INPUT, ValType.U32, count=2)

# --- Control (HOLDING) ------------------------------------------------------
# 0 = single phase, 1 = three phase. Firmware dependent; may be unavailable.
PHASE_SWITCH = RegisterDef("phase_switch", 405, RegType.HOLDING, ValType.U16, writable=True)
FAILSAFE_CURRENT_A = RegisterDef("failsafe_current_a", 2000, RegType.HOLDING, ValType.U16, writable=True)
FAILSAFE_TIMEOUT_S = RegisterDef("failsafe_timeout_s", 2002, RegType.HOLDING, ValType.U16, writable=True)
SET_CURRENT_A = RegisterDef("set_current_a", 5004, RegType.HOLDING, ValType.U16, writable=True)
ALIVE = RegisterDef("alive", 6000, RegType.HOLDING, ValType.U16, writable=True)


# --- OCPP 9-state mapping (Unite) ------------------------------------------
class ChargePointState(int, Enum):
    AVAILABLE = 0
    PREPARING = 1
    CHARGING = 2
    SUSPENDED_EVSE = 3
    SUSPENDED_EV = 4
    FINISHING = 5
    RESERVED = 6
    UNAVAILABLE = 7
    FAULTED = 8


CABLE_STATE_LABELS = {
    0: "disconnected",
    1: "cable_only",
    2: "vehicle_connected",
    3: "vehicle_locked",
}


# --- Block decode helpers ---------------------------------------------------
def block_u16(block: list[int], base: int, reg: RegisterDef) -> int:
    return int(block[reg.address - base])


def block_u32(block: list[int], base: int, reg: RegisterDef) -> int:
    offset = reg.address - base
    return int((block[offset] << 16) | block[offset + 1])


def decode_scalar(reg: RegisterDef, registers: list[int]) -> float | int | bool | str:
    """Decode a standalone register read into a Python value."""
    if reg.val_type == ValType.BOOL:
        return bool(registers[0])
    if reg.val_type == ValType.STRING:
        data = bytearray()
        for word in registers:
            data.extend(word.to_bytes(2, "big"))
        return data.decode("ascii", errors="ignore").strip("\x00 ").strip()
    if reg.val_type == ValType.S16:
        raw = registers[0]
        value = raw if raw < 0x8000 else raw - 0x10000
        return value * reg.scale
    if reg.val_type == ValType.U32:
        value = (registers[0] << 16) | registers[1]
        return value * reg.scale
    # U16
    return registers[0] * reg.scale


def encode_scalar(reg: RegisterDef, value: float | int | bool) -> list[int]:
    """Encode a Python value into Modbus register words for a write."""
    if reg.val_type == ValType.BOOL:
        return [1 if value else 0]
    if reg.val_type == ValType.U32:
        scaled = int(round(float(value) / reg.scale))
        return [(scaled >> 16) & 0xFFFF, scaled & 0xFFFF]
    # U16 / S16
    scaled = int(round(float(value) / reg.scale))
    return [scaled & 0xFFFF]
