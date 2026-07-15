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
from enum import Enum
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "unite_ev_charger"

# A bare package object whose __path__ points at the real source, but whose
# __init__.py is never executed (so no HA / pymodbus import).
_pkg = types.ModuleType("uec")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("uec", _pkg)

# Stub the handful of Home Assistant modules that controller.py / inputs.py
# import only for type hints, so they can be loaded without a full HA install.
for _mod in (
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.update_coordinator",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
sys.modules["homeassistant.config_entries"].ConfigEntry = object
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
sys.modules["homeassistant.core"].HomeAssistant = object


class _IssueSeverity(str, Enum):
    WARNING = "warning"


sys.modules["homeassistant.helpers.issue_registry"].IssueSeverity = _IssueSeverity
sys.modules["homeassistant.helpers.issue_registry"].async_create_issue = lambda *_args, **_kwargs: None
sys.modules["homeassistant.helpers.issue_registry"].async_delete_issue = lambda *_args, **_kwargs: None
sys.modules["homeassistant.helpers"].issue_registry = sys.modules[
    "homeassistant.helpers.issue_registry"
]


class _Store:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def async_load(self):
        return None

    async def async_save(self, _data) -> None:
        pass

    async def async_remove(self) -> None:
        pass


sys.modules["homeassistant.helpers.storage"].Store = _Store


class _ConfigEntryNotReady(Exception):
    pass


sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = _ConfigEntryNotReady


class _DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


class _UpdateFailed(Exception):
    pass


sys.modules[
    "homeassistant.helpers.update_coordinator"
].DataUpdateCoordinator = _DataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = _UpdateFailed

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
    "coordinator",
    "controller",
):
    _full = f"uec.{_name}"
    if _full not in sys.modules:
        _spec = importlib.util.spec_from_file_location(_full, PKG_DIR / f"{_name}.py")
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_full] = _mod
        _spec.loader.exec_module(_mod)

_integration_spec = importlib.util.spec_from_file_location(
    "uec.integration", PKG_DIR / "__init__.py"
)
_integration = importlib.util.module_from_spec(_integration_spec)
sys.modules["uec.integration"] = _integration
_integration_spec.loader.exec_module(_integration)
