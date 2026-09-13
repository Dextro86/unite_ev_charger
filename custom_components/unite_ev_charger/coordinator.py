"""Polling coordinator for the Unite EV Charger.

One block read for telemetry, one for the session, a heartbeat write, and -
when control is configured - one current write per cycle. That is at most a
handful of Modbus transactions every poll interval, instead of dozens.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import (
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_GRID_PHASES,
    CONF_HOST,
    CONF_PHASE_RESTORE_DELAY,
    CONF_PHASE_RESTORE_ON_UNPLUG,
    CONF_PHASE_SWITCHING,
    CONF_POLL_INTERVAL,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_PHASE_RESTORE_DELAY_S,
    DEFAULT_PHASE_RESTORE_ON_UNPLUG,
    DEFAULT_PHASE_SWITCHING,
    DEFAULT_POLL_INTERVAL,
    MAX_PHASE_RESTORE_DELAY_S,
    MIN_PHASE_RESTORE_DELAY_S,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DOMAIN,
)
from . import control as ctrl
from .control import effective_poll_interval
from .rest_client import (
    UnitePhpRestClient,
    UniteRestError,
    async_restore_three_phase,
    async_set_lockable_cable,
)
from .modbus import WebastoModbus, WebastoModbusError
from .models import DeviceInfo, WallboxData, apply_session, parse_telemetry
from .safety import capture_baseline, program_failsafe, restore_baseline, write_heartbeat

_LOGGER = logging.getLogger(__name__)


class WebastoCoordinator(DataUpdateCoordinator[WallboxData]):
    """Coordinates polling and (later) control for one wallbox."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WebastoModbus,
    ) -> None:
        self.entry = entry
        self.client = client
        self.device = DeviceInfo()
        self.controller = None  # wired in once control is built
        self._vehicle_was_connected = False
        self._auto_restore_task: asyncio.Task | None = None
        self._rfid_probe = ctrl.RfidProbe()
        self._baseline_store = Store(hass, 1, f"{DOMAIN}_baseline_{entry.entry_id}")
        self._baseline: dict[str, int | None] | None = None
        self._baseline_loaded = False
        # Lockable-cable installation setting (web UI). None = unknown
        # (no web UI login, or the firmware lacks the setting).
        self.lockable_cable: bool | None = None
        self.last_auto_phase_restore: datetime | None = None
        # monotonic deadline until which a web-UI reboot is considered in
        # progress (set by the restart button); drives the 'restarting' state.
        self.rest_restart_until: float | None = None
        # Last restart attempt (recorded by the button, no polling).
        self.rest_last_restart_at: datetime | None = None
        self.rest_last_restart_result: str | None = None  # success/auth_failed/unreachable

        poll = int(entry.options.get(
            CONF_POLL_INTERVAL,
            entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        ))
        failsafe_timeout = int(
            entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
        )
        # Never poll slower than the Alive heartbeat needs: the Unite drops the
        # socket + resets register 405 if Alive lapses past the failsafe timeout.
        effective_poll = effective_poll_interval(poll, failsafe_timeout)
        if effective_poll != poll:
            _LOGGER.debug(
                "Polling every %s s (configured %s s) to keep the Alive heartbeat "
                "within the %s s failsafe timeout",
                effective_poll,
                poll,
                failsafe_timeout,
            )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=effective_poll),
        )

    async def async_read_device_info(self) -> None:
        """Read static identity once. Best effort - missing fields are tolerated."""

        async def _try(reg: R.RegisterDef):
            try:
                return await self.client.read_register(reg)
            except WebastoModbusError as err:
                _LOGGER.debug("Optional identity register %s unavailable: %s", reg.name, err)
                return None

        self.device.serial_number = await _try(R.SERIAL_NUMBER)
        self.device.brand = await _try(R.BRAND)
        self.device.model = await _try(R.MODEL)
        self.device.firmware_version = await _try(R.FIRMWARE_VERSION)
        phases = await _try(R.NUMBER_OF_PHASES)
        if phases is not None:
            self.device.phases_supported = int(phases)
        min_a = await _try(R.MIN_CURRENT_HW_A)
        if min_a:
            self.device.min_current_a = int(min_a)
        max_a = await _try(R.MAX_CURRENT_CABLE_A)
        if max_a:
            self.device.max_current_a = int(max_a)

    async def _async_update_data(self) -> WallboxData:
        try:
            telemetry = await self.client.read_input_block(R.TELEMETRY_BASE, R.TELEMETRY_COUNT)
            session = await self.client.read_input_block(R.SESSION_BASE, R.SESSION_COUNT)
            data = parse_telemetry(telemetry)
            apply_session(data, session)

            # Single optional reads: best-effort, never drop the connection. A
            # failed probe costs one request, not a reconnect storm; a genuine
            # outage still surfaces through the block reads above.
            result = await self.client.try_read_optional(R.SET_CURRENT_A)
            data.set_current_a = int(result.value) if result.value is not None else None
            result = await self.client.try_read_optional(R.PHASE_SWITCH)
            data.phase_switch_raw = int(result.value) if result.value is not None else None
            # Re-read the phase capability (404) every cycle: a Unite can report
            # it wrong while booting, so a single setup read could permanently
            # (until reload) disable phase switching. Self-heal here; keep the
            # last good value on a failed read.
            result = await self.client.try_read_optional(R.NUMBER_OF_PHASES)
            if result.value is not None:
                data.phase_capability_raw = int(result.value)
                self.device.phases_supported = data.phase_capability_raw
            else:
                data.phase_capability_raw = self.device.phases_supported
            # Session RFID tag: only meaningful while a vehicle is connected, and
            # absent on firmware older than spec v1.9. Probed once per connection
            # (see RfidProbe in control.py): a failed probe never affects the
            # rest of the cycle and never drops the connection.
            if data.vehicle_connected and self._rfid_probe.want_probe:
                result = await self.client.try_read_optional(R.SESSION_RFID_TAG)
                if result.value is not None:
                    self._rfid_probe.note_ok()
                    tag = str(result.value).strip()
                    data.session_rfid = tag or None
                elif result.transport_error:
                    data.session_rfid = None
                    if self._rfid_probe.note_transport_error():
                        _LOGGER.warning(
                            "RFID probe keeps losing the connection; RFID reads "
                            "disabled until the integration is reloaded"
                        )
                else:
                    data.session_rfid = None
                    self._rfid_probe.note_unsupported()
                    _LOGGER.info(
                        "Charger does not serve the RFID registers; "
                        "skipping RFID reads on this connection"
                    )
            else:
                data.session_rfid = None

            self._maybe_auto_restore_phase(data)

            # First connect or a reconnect after the wallbox dropped us: the
            # Unite resets its failsafe + charging-current registers on every new
            # Modbus connection, so re-assert ownership before the heartbeat.
            if self.client.take_new_connection():
                await self._on_new_connection(data)

            # Keep the failsafe watchdog happy.
            await write_heartbeat(self.client)

            if self.controller is not None:
                try:
                    await self.controller.async_apply(data)
                except WebastoModbusError:
                    # A genuine Modbus failure: let it reach the outer handler so
                    # the connection is dropped and reclaimed next cycle.
                    raise
                except Exception:  # noqa: BLE001
                    # A control-logic hiccup (e.g. a stale/malformed meter sensor)
                    # must never blank out monitoring. Log it and keep serving the
                    # telemetry we already read; control retries next cycle.
                    _LOGGER.exception(
                        "Charge control step failed; keeping monitoring alive"
                    )

            return data
        except WebastoModbusError as err:
            raise UpdateFailed(str(err)) from err

    def _maybe_auto_restore_phase(self, data: WallboxData) -> None:
        """Re-apply the installation phase config after the vehicle is unplugged.

        The firmware only resets register 405 to its default on a power cycle,
        reset or Modbus disconnect - never on unplug - so on some chargers a new
        session starts single-phase even with 3-phase configured and every
        register reading correctly. Toggling currentLimiterPhase after each
        unplug forces the firmware to re-apply its default, giving the next
        session a clean start.

        Edge-triggered on the unplug: the fix would tear down a running session,
        so it only runs with no vehicle attached, and firing once per unplug
        means no pacing or retry counter - a failed attempt is retried at the
        next unplug.
        """
        connected = data.vehicle_connected
        just_unplugged = self._vehicle_was_connected and not connected
        self._vehicle_was_connected = connected
        if not just_unplugged:
            return
        o = self.entry.options
        if not ctrl.should_restore_phase_config(
            enabled=o.get(CONF_PHASE_RESTORE_ON_UNPLUG, DEFAULT_PHASE_RESTORE_ON_UNPLUG),
            rest_enabled=o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED),
            vehicle_connected=connected,
            phase_capability_raw=data.phase_capability_raw,
            grid_phases=o.get(CONF_GRID_PHASES),
        ):
            return
        if self._auto_restore_task is not None and not self._auto_restore_task.done():
            return
        self._auto_restore_task = self.hass.async_create_task(self._async_auto_restore_phase())

    async def _async_auto_restore_phase(self) -> None:
        o = self.entry.options
        delay = int(o.get(CONF_PHASE_RESTORE_DELAY, DEFAULT_PHASE_RESTORE_DELAY_S))
        delay = max(MIN_PHASE_RESTORE_DELAY_S, min(MAX_PHASE_RESTORE_DELAY_S, delay))
        # Let the charger finish ending the session and settle; also debounces a
        # bouncing cable state on unplug. Only re-check for a reconnect when we
        # actually waited: with no delay there is no window, and self.data may not
        # yet hold this poll's (just-unplugged) snapshot.
        if delay:
            await asyncio.sleep(delay)
            # A vehicle reconnected during the settle delay: toggling now would
            # drop register 404 to 0 for ~10 s and tear down the starting
            # session. Abort.
            if self.data is not None and self.data.vehicle_connected:
                _LOGGER.debug(
                    "Vehicle reconnected during the settle delay; skipping phase restore"
                )
                return
        try:
            route = await async_restore_three_phase(
                async_get_clientsession(self.hass),
                self.entry.data[CONF_HOST],
                o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                o.get(CONF_REST_PASSWORD, ""),
            )
        except UniteRestError as err:
            _LOGGER.warning(
                "Automatic phase-config restore after unplug failed "
                "(will retry at the next unplug): %s",
                err,
            )
            return
        self.last_auto_phase_restore = datetime.now(timezone.utc)
        _LOGGER.info(
            "Vehicle unplugged; re-applied the 3-phase config via %s so the next "
            "session starts clean",
            route,
        )
        await self.async_request_refresh()

    async def _on_new_connection(self, data: WallboxData) -> None:
        """Run the ownership handshake the wallbox expects on a fresh connection.

        Per the Vestel spec the wallbox resets failsafe, charging current *and*
        the phase register (405 -> 404 default) on every new Modbus connection.
        So we set failsafe current/timeout immediately, then let the controller
        restore the charging current and (in evcc mode) the requested phase. The
        freshly read ``phase_switch_raw`` is the reset default, used to skip a
        needless phase write when it already matches.
        """
        # A new connection may serve new firmware: allow one fresh RFID probe.
        # Strike history survives (it describes the wallbox, not the socket).
        self._rfid_probe.reset_on_reconnect()
        # Capture the pre-integration register values once, before our first
        # write. Stored durably, so a restart never mistakes our own values
        # for the originals.
        if not self._baseline_loaded:
            self._baseline_loaded = True
            try:
                stored = await self._baseline_store.async_load()
            except Exception:  # noqa: BLE001 - a corrupt store must not break setup
                stored = None
            if isinstance(stored, dict):
                self._baseline = {
                    str(k): (int(v) if isinstance(v, (int, float)) else None)
                    for k, v in stored.items()
                }
        if self._baseline is None:
            include_phase = bool(
                self.entry.options.get(CONF_PHASE_SWITCHING, DEFAULT_PHASE_SWITCHING)
            )
            self._baseline = await capture_baseline(self.client, include_phase=include_phase)
            try:
                await self._baseline_store.async_save(self._baseline)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not persist the register baseline", exc_info=True)
            _LOGGER.info("Captured charger register baseline: %s", self._baseline)
        await program_failsafe(
            self.client,
            failsafe_current_a=int(
                self.entry.options.get(CONF_FAILSAFE_CURRENT, DEFAULT_FAILSAFE_CURRENT_A)
            ),
            failsafe_timeout_s=int(
                self.entry.options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)
            ),
        )
        if self.controller is not None:
            await self.controller.async_on_reconnect(data.phase_switch_raw)

    def _webconfig_client(self) -> UnitePhpRestClient | None:
        """Webconfig client when the web UI login is configured, else None."""
        o = self.entry.options
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            return None
        return UnitePhpRestClient(
            self.entry.data.get(CONF_HOST, ""),
            o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
            o.get(CONF_REST_PASSWORD, ""),
        )

    async def async_refresh_lockable_cable(self) -> None:
        """Read the lockable-cable installation setting (best effort)."""
        client = self._webconfig_client()
        if client is None:
            return
        try:
            value = await client.get_lockable_cable()
        except UniteRestError as err:
            _LOGGER.debug("Could not read lockable-cable setting: %s", err)
            return
        if value is not None and value != self.lockable_cable:
            self.lockable_cable = value
            self.async_update_listeners()

    async def async_set_lockable_cable(self, enabled: bool) -> None:
        """Write the lockable-cable setting and verify by read-back.

        Writes prefer the JSON config API with webconfig fallback; verification
        reads back over webconfig (the JSON API has no read endpoint). When no
        read-back is possible, the acknowledged write stands.
        """
        o = self.entry.options
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            raise UniteRestError("Web UI login is not configured")
        route = await async_set_lockable_cable(
            async_get_clientsession(self.hass),
            self.entry.data.get(CONF_HOST, ""),
            o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
            o.get(CONF_REST_PASSWORD, ""),
            enabled,
        )
        value: bool | None = None
        php = self._webconfig_client()
        if php is not None:
            try:
                value = await php.get_lockable_cable()
            except UniteRestError as err:
                _LOGGER.debug("Could not verify the lockable-cable setting: %s", err)
        if value is None:
            _LOGGER.debug("Set lockable cable via %s without read-back", route)
            value = enabled
        self.lockable_cable = value
        self.async_update_listeners()
        if value != enabled:
            raise UniteRestError("Charger did not take the lockable-cable setting")

    async def async_restore_baseline_on_exit(self) -> None:
        """Write the captured pre-integration values back (best effort).

        Called on unload, removal and shutdown. Never raises; anything that
        cannot be restored is logged with its value for manual recovery.
        """
        baseline = self._baseline
        if baseline is None:
            try:
                stored = await self._baseline_store.async_load()
            except Exception:  # noqa: BLE001
                stored = None
            baseline = dict(stored) if isinstance(stored, dict) else None
        if not baseline or all(v is None for v in baseline.values()):
            return
        failed = await restore_baseline(self.client, baseline)
        if failed:
            _LOGGER.warning(
                "Could not restore charger registers; recover manually: %s",
                {key: baseline.get(key) for key in failed},
            )
        else:
            _LOGGER.info("Restored charger register baseline on exit")

    @property
    def device_unique_id(self) -> str:
        # Must be STABLE for the life of the config entry: HA keys the device
        # and every entity on this value. `entry.unique_id` is captured once at
        # config time (serial, or host:port) and never changes. Deriving it from
        # the runtime `device.serial_number` instead caused a duplicate device on
        # restart whenever that read transiently returned nothing (charger still
        # booting / Modbus busy) and it fell back to entry_id.
        return self.entry.unique_id or self.entry.entry_id
