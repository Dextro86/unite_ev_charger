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


class _DataUpdateCoordinator:
    """Minimal stand-in: the coordinator tests drive _async_update_data directly."""

    def __init_subclass__(cls, *args, **kwargs) -> None:
        # Swallow the [WallboxData] subscription: plain object subclassing.
        super().__init_subclass__()

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True

    async def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


class _UpdateFailed(Exception):
    pass


class _Store:
    def __init__(self, hass, version, key):
        self._hass = hass
        self._key = key

    async def async_load(self):
        return None

    async def async_save(self, data) -> None:
        pass


_helpers = types.ModuleType("homeassistant.helpers")
_helpers.__path__ = []
sys.modules.setdefault("homeassistant.helpers", _helpers)

_helpers_aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")
_helpers_aiohttp.async_get_clientsession = lambda hass: None
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", _helpers_aiohttp)

_helpers_storage = types.ModuleType("homeassistant.helpers.storage")
_helpers_storage.Store = _Store
sys.modules.setdefault("homeassistant.helpers.storage", _helpers_storage)

_helpers_update = types.ModuleType("homeassistant.helpers.update_coordinator")
_helpers_update.DataUpdateCoordinator = _DataUpdateCoordinator
_helpers_update.UpdateFailed = _UpdateFailed
sys.modules.setdefault("homeassistant.helpers.update_coordinator", _helpers_update)


def _async_track_time_interval(hass, action, interval):
    return lambda: None


_helpers_event = types.ModuleType("homeassistant.helpers.event")
_helpers_event.async_track_time_interval = _async_track_time_interval
sys.modules.setdefault("homeassistant.helpers.event", _helpers_event)

# Pure(-ish) modules that are safe to import this way (HA only via the stubs).
# Order matters: a module must be loaded before others that import it.
for _name in (
    "const",
    "units",
    "registers",
    "models",
    "control",
    "modbus",
    "inputs",
    "eventlog",
    "safety",
    "rest_client",
    "evcc_watchdog",
    "controller",
    "phase",
    "coordinator",
):
    _full = f"uec.{_name}"
    if _full not in sys.modules:
        _spec = importlib.util.spec_from_file_location(_full, PKG_DIR / f"{_name}.py")
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_full] = _mod
        _spec.loader.exec_module(_mod)
