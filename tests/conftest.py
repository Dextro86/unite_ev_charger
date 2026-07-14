"""Test bootstrap.

The integration package's __init__.py imports Home Assistant and pymodbus, which
we deliberately do not want in the fast pure-logic tests. So we load the
HA-free modules (registers, models, and later the control engine) in isolation
under a lightweight package alias ``uec``, without executing __init__.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "unite_ev_charger"

# A bare package object whose __path__ points at the real source, but whose
# __init__.py is never executed (so no HA / pymodbus import).
_pkg = types.ModuleType("uec")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("uec", _pkg)

# Stub the handful of Home Assistant modules that controller.py / inputs.py
# import only for type hints, so they can be loaded without a full HA install.
for _mod in ("homeassistant", "homeassistant.config_entries", "homeassistant.core"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
sys.modules["homeassistant.config_entries"].ConfigEntry = object
sys.modules["homeassistant.core"].HomeAssistant = object

# Pure(-ish) modules that are safe to import this way (HA only via the stubs).
# Order matters: a module must be loaded before others that import it.
for _name in (
    "const",
    "units",
    "registers",
    "models",
    "control",
    "modbus",
    "safety",
    "inputs",
    "controller",
):
    _full = f"uec.{_name}"
    if _full not in sys.modules:
        _spec = importlib.util.spec_from_file_location(_full, PKG_DIR / f"{_name}.py")
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_full] = _mod
        _spec.loader.exec_module(_mod)
