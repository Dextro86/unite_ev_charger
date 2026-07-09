"""Unit tests for the verified register map decode/encode helpers."""
from __future__ import annotations

from uec import registers as R
from uec.models import WallboxData, apply_session, parse_telemetry
from uec.registers import ChargePointState


def _make_telemetry_block() -> list[int]:
    """Build a 1000..1037 block with known values at the right offsets."""
    block = [0] * R.TELEMETRY_COUNT
    base = R.TELEMETRY_BASE

    def put_u16(reg: R.RegisterDef, raw: int) -> None:
        block[reg.address - base] = raw

    def put_u32(reg: R.RegisterDef, raw: int) -> None:
        off = reg.address - base
        block[off] = (raw >> 16) & 0xFFFF
        block[off + 1] = raw & 0xFFFF

    put_u16(R.CHARGE_POINT_STATE, int(ChargePointState.CHARGING))
    put_u16(R.CHARGING_STATE, 1)
    put_u16(R.CABLE_STATE, 2)  # vehicle connected
    put_u16(R.CURRENT_L1_A, 16000)  # 16.0 A in mA
    put_u16(R.CURRENT_L2_A, 0)
    put_u16(R.CURRENT_L3_A, 0)
    put_u16(R.VOLTAGE_L1_V, 230)
    put_u32(R.ACTIVE_POWER_W, 3680)  # 16 A * 230 V
    put_u32(R.METER_ENERGY_KWH, 12345)  # *0.1 => 1234.5 kWh
    return block


def test_telemetry_decode():
    data = parse_telemetry(_make_telemetry_block())
    assert data.state == ChargePointState.CHARGING
    assert data.charging is True
    assert data.vehicle_connected is True
    assert data.current_l1_a == 16.0
    assert data.actual_current_a == 16.0
    assert data.phases_in_use == 1
    assert data.active_power_w == 3680
    assert data.meter_energy_kwh == 1234.5


def test_session_decode():
    block = [0] * R.SESSION_COUNT
    base = R.SESSION_BASE
    # session energy 1502 (uint32, *0.001 kWh) -> 5000 raw = 5.0 kWh... actually Wh
    off = R.SESSION_ENERGY_KWH.address - base
    block[off], block[off + 1] = 0, 5000  # 5000 * 0.001 = 5.0
    doff = R.SESSION_DURATION_S.address - base
    block[doff], block[doff + 1] = 0, 3600
    data = parse_telemetry(_make_telemetry_block())
    apply_session(data, block)
    assert data.session_energy_kwh == 5.0
    assert data.session_duration_s == 3600


def test_string_decode():
    # "AB" -> 0x4142
    words = [0x4142, 0x4300]  # "ABC\x00"
    assert R.decode_scalar(R.MODEL, words) == "ABC"


def test_encode_u16_and_u32():
    assert R.encode_scalar(R.SET_CURRENT_A, 16) == [16]
    # u32 scaled meter not writable, but exercise encoder via a u32 reg shape
    assert R.encode_scalar(R.ACTIVE_POWER_W, 70000) == [(70000 >> 16) & 0xFFFF, 70000 & 0xFFFF]


def test_cable_state_labels():
    assert R.CABLE_STATE_LABELS[2] == "vehicle_connected"


def test_iec61851_status():
    d = WallboxData()
    assert d.iec61851_status == "A"  # no vehicle
    d.cable_state_raw = 2
    assert d.iec61851_status == "B"  # connected, not charging
    d.charge_point_state_raw = int(ChargePointState.CHARGING)
    assert d.iec61851_status == "C"  # charging
    d.charge_point_state_raw = int(ChargePointState.FAULTED)
    assert d.iec61851_status == "F"  # fault takes precedence
