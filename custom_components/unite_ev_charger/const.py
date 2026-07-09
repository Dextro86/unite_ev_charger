"""Constants for the Unite EV Charger integration.

Only connection + safety basics live here for now. Feature-specific config keys
(DLB, Solar, phase switching) are added as we agree on the feature set.
"""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "unite_ev_charger"

# --- Connection -------------------------------------------------------------
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_UNIT_ID: Final = "unit_id"
CONF_POLL_INTERVAL: Final = "poll_interval"

DEFAULT_PORT: Final = 502
# Vestel/Webasto firmware answers on Modbus unit id 255.
DEFAULT_UNIT_ID: Final = 255
DEFAULT_POLL_INTERVAL: Final = 10  # seconds; the wallbox does not need 2s polling
MIN_POLL_INTERVAL: Final = 5
MAX_POLL_INTERVAL: Final = 60

# --- Current limits ---------------------------------------------------------
ABS_MIN_CURRENT_A: Final = 6
ABS_MAX_CURRENT_A: Final = 32

# --- Failsafe / heartbeat ---------------------------------------------------
# If the charger receives no alive write (reg 6000) within the failsafe timeout
# it falls back to the failsafe current. This is what makes HA-side current
# control safe: lose HA/network and the charger stops on its own.
DEFAULT_FAILSAFE_CURRENT_A: Final = 6  # keep trickle-charging if HA/network drops
DEFAULT_FAILSAFE_TIMEOUT_S: Final = 30
HEARTBEAT_ALIVE_VALUE: Final = 1

# --- Platforms --------------------------------------------------------------
PLATFORMS: Final = ["sensor", "binary_sensor", "switch", "select", "number", "button"]

# --- Charge modes (language-independent keys) -------------------------------
MODE_FAST: Final = "fast"
MODE_MANUAL: Final = "manual"
MODE_SOLAR: Final = "solar"
MODE_MIN_SOLAR: Final = "min_solar"
CHARGE_MODES: Final = (MODE_FAST, MODE_MANUAL, MODE_SOLAR, MODE_MIN_SOLAR)
SOLAR_MODES: Final = (MODE_SOLAR, MODE_MIN_SOLAR)
DEFAULT_MODE: Final = MODE_FAST

# --- Control tuning (hardcoded on purpose, not user-configurable) -----------
SOLAR_SMOOTHING_S: Final = 120          # rolling average window for surplus
SOLAR_DEADBAND_A: Final = 1             # only rewrite setpoint on >= 1 A change
SOLAR_MIN_CHARGE_DURATION_S: Final = 300  # keep charging >= 5 min once started
SOLAR_STOP_HYSTERESIS_A: Final = 1       # stop a touch below min to avoid flapping

NOMINAL_VOLTAGE: Final = 230
PLAUSIBLE_VOLTAGE_MIN: Final = 200
PLAUSIBLE_VOLTAGE_MAX: Final = 260

# --- DLB defaults -----------------------------------------------------------
DEFAULT_DLB_MARGIN_A: Final = 2
DEFAULT_MAIN_FUSE_A: Final = 25

DEFAULT_MIN_CURRENT_A: Final = 6
DEFAULT_MAX_CURRENT_A: Final = 16

# --- Charging control owner -------------------------------------------------
# internal = our own solar/DLB/manual control loop drives the charger.
# external = a controller like evcc drives it; our loop stays passive and the
#            entities write straight through to the wallbox.
CONF_CONTROL_MODE: Final = "control_mode"
CONTROL_INTERNAL: Final = "internal"
CONTROL_EXTERNAL: Final = "external"
CONTROL_MODES: Final = (CONTROL_INTERNAL, CONTROL_EXTERNAL)
DEFAULT_CONTROL_MODE: Final = CONTROL_INTERNAL

# --- Option keys (shared by controller + options flow) ----------------------
CONF_DEFAULT_MODE: Final = "default_mode"
CONF_MIN_CURRENT: Final = "min_current"
CONF_MAX_CURRENT: Final = "max_current"
CONF_RESET_ON_DISCONNECT: Final = "reset_on_disconnect"

# Power meter (shared by Solar + DLB)
CONF_METER_MODEL: Final = "meter_model"
CONF_GRID_POWER_SENSOR: Final = "grid_power_sensor"
CONF_GRID_EXPORT_NEGATIVE: Final = "grid_export_negative"
CONF_IMPORT_SENSOR: Final = "import_power_sensor"
CONF_EXPORT_SENSOR: Final = "export_power_sensor"
CONF_SURPLUS_SENSOR: Final = "surplus_power_sensor"
CONF_NOMINAL_VOLTAGE: Final = "nominal_voltage"

METER_NONE: Final = "none"
METER_SIGNED_GRID: Final = "signed_grid"   # HomeWizard P1 / other single signed sensor
METER_DSMR: Final = "dsmr"                  # separate import + export
METER_SURPLUS: Final = "surplus"           # ready-made surplus sensor
METER_MODELS: Final = (METER_NONE, METER_SIGNED_GRID, METER_DSMR, METER_SURPLUS)

# Solar
CONF_SOLAR_MIN_CURRENT: Final = "solar_min_current"
CONF_PHASE_SWITCH_DWELL: Final = "phase_switch_dwell"
DEFAULT_PHASE_SWITCH_DWELL_S: Final = 300

# Master opt-in for phase management (register 405 is firmware-dependent, so
# off by default). When off the integration never writes 405 and the phase
# preference select is hidden.
CONF_PHASE_SWITCHING: Final = "phase_switching"
DEFAULT_PHASE_SWITCHING: Final = False
PHASE_SWITCH_HYSTERESIS: Final = 0.9   # drop to 1p below 0.9 x the 3p-minimum

# --- Phase preference (live select, internal mode) --------------------------
# Auto = the integration decides: solar modes follow surplus, fast/manual use
# all available phases. 1/3 force a fixed phase. Persists (unlike the mode,
# which resets on disconnect).
PHASE_AUTO: Final = "auto"
PHASE_1P: Final = "1"
PHASE_3P: Final = "3"
PHASE_PREFERENCES: Final = (PHASE_AUTO, PHASE_1P, PHASE_3P)
DEFAULT_PHASE_PREFERENCE: Final = PHASE_AUTO
# After writing register 405, block a new switch for at least this long (the
# wallbox needs time to settle the CP interruption it performs internally).
PHASE_SWITCH_SETTLE_MIN_S: Final = 15
# Right after a phase switch, skip writing the charge-current setpoint (5004)
# for this long, so the wallbox can complete its internal CP interruption
# undisturbed. The heartbeat keeps going; only the current write is held back.
PHASE_SWITCH_QUIET_S: Final = 20

# Phase switching is a single register-405 write in every case (plug-in or
# during charging); the Unite runs its own IEC CP interruption. This mirrors
# evcc's proven Vestel handling - no stop/hold "recovery" sequence.

# --- Optional adaptive 1->3 phase recovery ----------------------------------
# Some cars (verified on real hardware) cache their 1p/3p choice per session and
# refuse a live 1->3 upshift while charging - neither a plain 405 write nor evcc
# can force it. The only thing that works is a long charging pause: the car must
# sit at 0 A / IEC status B long enough (measured: >61 s, 91 s worked) to
# re-negotiate. Opt-in, off by default, because it deliberately interrupts
# charging. When on: first try the normal live switch and observe; only if the
# car is still physically single-phase do we force the pause. We do NOT write
# 405 again after the pause - the register is already 3P; only the pause matters.
CONF_PHASE_RECOVERY_ENABLED: Final = "phase_recovery_enabled"
CONF_PHASE_RECOVERY_OBSERVE: Final = "phase_recovery_observe"
CONF_PHASE_RECOVERY_DWELL: Final = "phase_recovery_dwell"
DEFAULT_PHASE_RECOVERY_ENABLED: Final = False
DEFAULT_PHASE_RECOVERY_OBSERVE_S: Final = 60
DEFAULT_PHASE_RECOVERY_DWELL_S: Final = 121  # > the measured 91 s threshold, with margin
PHASE_RECOVERY_SETTLE_S: Final = 3  # brief settle before re-energising after the pause

# Judge the REAL phase count from measured per-phase current (not register 405,
# which only says what the charger is set to, not what the car actually draws).
PHASE_MEASURE_ON_A: Final = 3.0
PHASE_MEASURE_OFF_A: Final = 2.0

# Recovery status values (also used as translation keys for the diagnostic sensor).
RECOVERY_IDLE: Final = "idle"
RECOVERY_OBSERVING: Final = "observing_3p"
RECOVERY_DWELLING: Final = "dwelling"
RECOVERY_RESUMING: Final = "resuming"
RECOVERY_COMPLETE: Final = "complete"
RECOVERY_ABORTED: Final = "aborted"

# --- Interpreted charger state (State Inspector) ----------------------------
# One sensor that interprets what the wallbox is doing, composed from state we
# already track (connection, recovery, phase, fault). Only reliably-derivable
# states - no "firmware suspected" heuristic.
CHARGER_STATES: Final = (
    "idle",           # no vehicle (status A)
    "connected",      # plugged in, not charging (status B)
    "charging",       # charging normally
    "phase_mismatch", # charging, but set to 3-phase while drawing 1-phase
    "recovery",       # a phase-recovery sequence is running
    "restarting",     # a web-UI reboot was triggered recently
    "disconnected",   # Modbus poll is failing (offline / failsafe)
    "fault",          # the charger reports a fault
)

# DLB
CONF_DLB_ENABLED: Final = "dlb_enabled"
CONF_MAIN_FUSE_A: Final = "main_fuse_a"
CONF_DLB_MARGIN_A: Final = "dlb_margin_a"
CONF_DLB_CURRENT_L1: Final = "dlb_current_l1"
CONF_DLB_CURRENT_L2: Final = "dlb_current_l2"
CONF_DLB_CURRENT_L3: Final = "dlb_current_l3"

# Advanced
CONF_FAILSAFE_CURRENT: Final = "failsafe_current"
CONF_FAILSAFE_TIMEOUT: Final = "failsafe_timeout"

# --- REST web API (opt-in; only used for the restart button) ----------------
# The wallbox can be rebooted via its web UI. Modbus has no reboot register, so
# this is the one thing we do over HTTPS. Fully isolated from the Modbus control
# path: if REST fails, charging control is unaffected.
CONF_REST_ENABLED: Final = "rest_enabled"
CONF_REST_USERNAME: Final = "rest_username"
CONF_REST_PASSWORD: Final = "rest_password"
DEFAULT_REST_ENABLED: Final = False
DEFAULT_REST_USERNAME: Final = "admin"
# Measured on real hardware: after a reboot the web UI is unreachable for ~5 min
# (Modbus recovers a bit sooner). Block repeat presses for that window so a
# second press gives a clear "wait" instead of a connection error.
REST_RESTART_COOLDOWN_S: Final = 300
