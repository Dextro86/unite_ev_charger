"""Config + options flow for Unite EV Charger.

The setup step is deliberately minimal and bounded (just a connection test, so
it cannot hang). All feature configuration lives in the options flow, which is
menu-driven: you edit one section, return to the menu, edit another, and save
once at the end. Power/current sensors are picked with filtered entity
selectors, and we additionally reject energy sensors - the exact mistake that
used to crash the old integration.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import registers as R
from .const import (
    CHARGE_MODES,
    CONF_CONTROL_MODE,
    CONF_DEFAULT_MODE,
    CONF_GRID_PHASES,
    CONF_DLB_CURRENT_L1,
    CONF_DLB_CURRENT_L2,
    CONF_DLB_CURRENT_L3,
    CONF_DLB_ENABLED,
    CONF_DLB_MARGIN_A,
    CONF_EXPORT_SENSOR,
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_GRID_EXPORT_NEGATIVE,
    CONF_GRID_POWER_SENSOR,
    CONF_HOST,
    CONF_IMPORT_SENSOR,
    CONF_MAIN_FUSE_A,
    CONF_MAX_CURRENT,
    CONF_METER_MODEL,
    CONF_MIN_CURRENT,
    CONF_NOMINAL_VOLTAGE,
    CONF_PHASE_RESTORE_ON_UNPLUG,
    CONF_PHASE_RECOVERY_DWELL,
    CONF_PHASE_RECOVERY_ENABLED,
    CONF_PHASE_RECOVERY_OBSERVE,
    CONF_PHASE_SWITCH_DWELL,
    CONF_PHASE_SWITCHING,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    CONF_SOLAR_MIN_CURRENT,
    CONF_SURPLUS_SENSOR,
    CONF_UNIT_ID,
    CONTROL_MODES,
    DEFAULT_CONTROL_MODE,
    DEFAULT_DLB_MARGIN_A,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_MAIN_FUSE_A,
    DEFAULT_MAX_CURRENT_A,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_MODE,
    DEFAULT_PHASE_RESTORE_ON_UNPLUG,
    DEFAULT_PHASE_RECOVERY_DWELL_S,
    DEFAULT_PHASE_RECOVERY_ENABLED,
    DEFAULT_PHASE_RECOVERY_OBSERVE_S,
    DEFAULT_PHASE_SWITCH_DWELL_S,
    DEFAULT_PHASE_SWITCHING,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DEFAULT_UNIT_ID,
    DOMAIN,
    GRID_PHASES,
    GRID_PHASES_1,
    GRID_PHASES_3,
    MAX_POLL_INTERVAL,
    METER_DSMR,
    METER_MODELS,
    METER_SIGNED_GRID,
    METER_SURPLUS,
    MIN_POLL_INTERVAL,
    NOMINAL_VOLTAGE,
)
from .modbus import WebastoModbus, WebastoModbusError
from .rest_client import UniteRestAuthError, UniteRestError, async_build_rest_client
from .units import ENERGY_UNITS

_POWER_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_CURRENT_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="current")
)


def _mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(CHARGE_MODES),
            translation_key="charge_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _meter_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(METER_MODELS),
            translation_key="meter_model",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _control_mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(CONTROL_MODES),
            translation_key="control_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _grid_phases_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(GRID_PHASES),
            translation_key="grid_phases",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _num(minv: float, maxv: float, step: float = 1, unit: str | None = None) -> selector.NumberSelector:
    """A clean number input box (with unit), instead of a slider."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minv,
            max=maxv,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class UniteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial connection setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            client = WebastoModbus(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                unit_id=user_input[CONF_UNIT_ID],
            )
            serial: Any = None
            try:
                serial = await client.read_register(R.SERIAL_NUMBER)
            except WebastoModbusError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - never let setup hang/crash
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                serial_str = str(serial).strip() if serial else ""
                unique = serial_str or f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = user_input.get(CONF_NAME) or "Unite EV Charger"
                return self.async_create_entry(title=title, data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Unite EV Charger"): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_UNIT_ID, default=DEFAULT_UNIT_ID): int,
            }
        )
        data_schema = self.add_suggested_values_to_schema(schema, user_input or {})
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the connection (IP / port / unit id) of an existing charger."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])

        if user_input is not None:
            client = WebastoModbus(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                unit_id=user_input[CONF_UNIT_ID],
            )
            try:
                await client.read_register(R.SERIAL_NUMBER)
            except WebastoModbusError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **user_input}
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_UNIT_ID, default=DEFAULT_UNIT_ID): int,
            }
        )
        suggested = {**(entry.data if entry else {}), **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return UniteOptionsFlow(config_entry)


class UniteOptionsFlow(OptionsFlow):
    """Menu-driven settings: edit sections, then save once.

    Each section updates an in-memory copy of the options and returns to the
    menu, so you can walk through everything in one sitting. Nothing is written
    until you pick 'Save & close'.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self.options: dict[str, Any] = dict(config_entry.options)
        # Connection edits (host/port/unit) live in entry.data, not options, so
        # they are staged here and applied once on save.
        self._data_updates: dict[str, Any] = {}

    def _default_grid_phases(self) -> str:
        """Default the wiring question to what the charger reports (register 404).

        A running coordinator knows ``phases_supported``; fall back to 3-phase
        only when that is unknown. An explicit user choice always wins, because
        404 = 0 cannot distinguish a 1-phase install from a stuck 3-phase one.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        supported = getattr(getattr(coordinator, "device", None), "phases_supported", None)
        if supported == 0:
            return GRID_PHASES_1
        return GRID_PHASES_3

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["connection", "charge", "meter", "dlb", "solar", "advanced", "reboot", "save"],
        )

    async def async_step_reboot(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Opt-in web-UI access for the restart button (username/password)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_REST_ENABLED):
                try:
                    client = await async_build_rest_client(
                        async_get_clientsession(self.hass),
                        self._entry.data.get(CONF_HOST),
                        user_input.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                        user_input.get(CONF_REST_PASSWORD, ""),
                    )
                    await client.test_connection()
                except UniteRestAuthError:
                    errors["base"] = "invalid_auth"
                except UniteRestError:
                    errors["base"] = "rest_cannot_connect"
            if not errors:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_REST_ENABLED, default=DEFAULT_REST_ENABLED): bool,
                vol.Required(CONF_REST_USERNAME, default=DEFAULT_REST_USERNAME): str,
                vol.Optional(CONF_REST_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reboot",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    async def async_step_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the connection (IP / port / unit id) from the settings menu."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = WebastoModbus(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                unit_id=user_input[CONF_UNIT_ID],
            )
            try:
                await client.read_register(R.SERIAL_NUMBER)
            except WebastoModbusError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - never let the flow hang/crash
                errors["base"] = "unknown"
            finally:
                await client.async_close()
            if not errors:
                self._data_updates.update(user_input)
                return await self.async_step_init()
        current = {**self._entry.data, **self._data_updates}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_UNIT_ID, default=DEFAULT_UNIT_ID): int,
            }
        )
        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._data_updates:
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, **self._data_updates}
            )
        return self.async_create_entry(title="", data=self.options)

    def _energy_error(self, *entity_ids: str | None) -> bool:
        for entity_id in entity_ids:
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state and state.attributes.get("unit_of_measurement") in ENERGY_UNITS:
                return True
        return False

    # -- charge settings ----------------------------------------------------
    async def async_step_charge(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_MIN_CURRENT] > user_input[CONF_MAX_CURRENT]:
                errors["base"] = "min_above_max"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_CONTROL_MODE, default=DEFAULT_CONTROL_MODE): _control_mode_selector(),
                vol.Required(CONF_DEFAULT_MODE, default=DEFAULT_MODE): _mode_selector(),
                vol.Required(CONF_MIN_CURRENT, default=DEFAULT_MIN_CURRENT_A): _num(6, 32, 1, "A"),
                vol.Required(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT_A): _num(6, 32, 1, "A"),
                vol.Required(
                    CONF_GRID_PHASES, default=self._default_grid_phases()
                ): _grid_phases_selector(),
                vol.Required(CONF_PHASE_SWITCHING, default=DEFAULT_PHASE_SWITCHING): bool,
                vol.Required(
                    CONF_PHASE_RECOVERY_ENABLED, default=DEFAULT_PHASE_RECOVERY_ENABLED
                ): bool,
                vol.Required(
                    CONF_PHASE_RECOVERY_OBSERVE, default=DEFAULT_PHASE_RECOVERY_OBSERVE_S
                ): _num(20, 120, 1, "s"),
                vol.Required(
                    CONF_PHASE_RECOVERY_DWELL, default=DEFAULT_PHASE_RECOVERY_DWELL_S
                ): _num(60, 300, 1, "s"),
                vol.Required(
                    CONF_PHASE_RESTORE_ON_UNPLUG, default=DEFAULT_PHASE_RESTORE_ON_UNPLUG
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="charge",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    # -- power meter (two steps) -------------------------------------------
    async def async_step_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            model = user_input[CONF_METER_MODEL]
            self.options[CONF_METER_MODEL] = model
            if model == METER_SIGNED_GRID:
                return await self.async_step_meter_signed()
            if model == METER_DSMR:
                return await self.async_step_meter_dsmr()
            if model == METER_SURPLUS:
                return await self.async_step_meter_surplus()
            return await self.async_step_init()  # METER_NONE
        schema = vol.Schema({vol.Required(CONF_METER_MODEL): _meter_selector()})
        return self.async_show_form(
            step_id="meter",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
        )

    async def async_step_meter_signed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._energy_error(user_input.get(CONF_GRID_POWER_SENSOR)):
                errors["base"] = "not_power"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_GRID_POWER_SENSOR): _POWER_SENSOR,
                vol.Required(CONF_GRID_EXPORT_NEGATIVE, default=True): bool,
                vol.Required(CONF_NOMINAL_VOLTAGE, default=NOMINAL_VOLTAGE): _num(200, 260, 1, "V"),
            }
        )
        return self.async_show_form(
            step_id="meter_signed",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    async def async_step_meter_dsmr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._energy_error(
                user_input.get(CONF_IMPORT_SENSOR), user_input.get(CONF_EXPORT_SENSOR)
            ):
                errors["base"] = "not_power"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_SENSOR): _POWER_SENSOR,
                vol.Required(CONF_EXPORT_SENSOR): _POWER_SENSOR,
                vol.Required(CONF_NOMINAL_VOLTAGE, default=NOMINAL_VOLTAGE): _num(200, 260, 1, "V"),
            }
        )
        return self.async_show_form(
            step_id="meter_dsmr",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    async def async_step_meter_surplus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._energy_error(user_input.get(CONF_SURPLUS_SENSOR)):
                errors["base"] = "not_power"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_SURPLUS_SENSOR): _POWER_SENSOR,
                vol.Required(CONF_NOMINAL_VOLTAGE, default=NOMINAL_VOLTAGE): _num(200, 260, 1, "V"),
            }
        )
        return self.async_show_form(
            step_id="meter_surplus",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    # -- DLB ----------------------------------------------------------------
    async def async_step_dlb(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_DLB_ENABLED) and not user_input.get(CONF_DLB_CURRENT_L1):
                errors["base"] = "dlb_needs_l1"
            else:
                self.options.update(user_input)
                return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_DLB_ENABLED, default=False): bool,
                vol.Required(CONF_MAIN_FUSE_A, default=DEFAULT_MAIN_FUSE_A): _num(6, 125, 1, "A"),
                vol.Required(CONF_DLB_MARGIN_A, default=DEFAULT_DLB_MARGIN_A): _num(0, 16, 1, "A"),
                vol.Optional(CONF_DLB_CURRENT_L1): _CURRENT_SENSOR,
                vol.Optional(CONF_DLB_CURRENT_L2): _CURRENT_SENSOR,
                vol.Optional(CONF_DLB_CURRENT_L3): _CURRENT_SENSOR,
            }
        )
        return self.async_show_form(
            step_id="dlb",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
            errors=errors,
        )

    # -- solar --------------------------------------------------------------
    async def async_step_solar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_SOLAR_MIN_CURRENT, default=DEFAULT_MIN_CURRENT_A): _num(6, 32, 1, "A"),
                vol.Required(CONF_PHASE_SWITCH_DWELL, default=DEFAULT_PHASE_SWITCH_DWELL_S): _num(60, 1800, 10, "s"),
            }
        )
        return self.async_show_form(
            step_id="solar",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
        )

    # -- advanced -----------------------------------------------------------
    async def async_step_advanced(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): _num(
                    MIN_POLL_INTERVAL, MAX_POLL_INTERVAL, 1, "s"
                ),
                vol.Required(CONF_FAILSAFE_CURRENT, default=DEFAULT_FAILSAFE_CURRENT_A): _num(0, 32, 1, "A"),
                vol.Required(CONF_FAILSAFE_TIMEOUT, default=DEFAULT_FAILSAFE_TIMEOUT_S): _num(10, 120, 1, "s"),
            }
        )
        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
        )
