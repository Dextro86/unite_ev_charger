"""Adaptive phase fixes: recovery (1->3) and downshift (3->1).

Mixin for ChargeControl: the observe-pause-resume machine both directions
share, plus the gates that decide when a stuck car needs one. All state
(latches, task, status) lives on the controller; this module only holds the
behaviour, so the controller file stays about deciding, not pausing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING, Any

from . import registers as R
from .const import (
    ABS_MAX_CURRENT_A,
    PHASE_MEASURE_OFF_A,
    PHASE_MEASURE_ON_A,
    PHASE_RECOVERY_SETTLE_S,
    RECOVERY_ABORTED,
    RECOVERY_COMPLETE,
    RECOVERY_DWELLING,
    RECOVERY_OBSERVING,
    RECOVERY_RESUMING,
)
from .models import WallboxData

if TYPE_CHECKING:
    from .controller import ControlConfig
    from .coordinator import WebastoCoordinator

_LOGGER = logging.getLogger(__name__)


class PhaseRecoveryMixin:
    """Observe-pause-resume machine, both phase directions."""

    # State below lives on the controller; declared here only so type
    # checkers see what the mixin expects from its host class.
    cfg: ControlConfig
    coordinator: WebastoCoordinator
    _recovery_task: asyncio.Task | None
    _recovery_status: str
    _recovery_remaining_s: int
    _recovery_attempted: bool
    _downshift_attempted: bool
    _buffer_commands: bool
    _last_recovery_at: Any
    _last_recovery_result: str | None
    _enabled_intent: bool | None
    _current_intent: int | None
    _ext_resume_current: int
    _last_setpoint: int | None

    # -- gates ---------------------------------------------------------------
    @staticmethod
    def _measured_single_phase(data: WallboxData) -> bool:
        return (
            data.current_l1_a >= PHASE_MEASURE_ON_A
            and data.current_l2_a < PHASE_MEASURE_OFF_A
            and data.current_l3_a < PHASE_MEASURE_OFF_A
        )

    @staticmethod
    def _measured_three_phase(data: WallboxData) -> bool:
        return (
            data.current_l1_a >= PHASE_MEASURE_ON_A
            and data.current_l2_a >= PHASE_MEASURE_ON_A
            and data.current_l3_a >= PHASE_MEASURE_ON_A
        )

    def _should_start_recovery(self, data: WallboxData | None) -> bool:
        """Recovery only when it is enabled, not already tried this request, and
        the car is genuinely charging on a single phase."""
        if not self.cfg.phase_recovery_enabled:
            return False
        if self.recovery_active or self._recovery_attempted or data is None:
            return False
        if not data.vehicle_connected or not data.charging:
            return False
        return data.phase_switch_raw == 0 or self._measured_single_phase(data)

    def _should_start_downshift(self, data: WallboxData | None) -> bool:
        """Downshift mirror of recovery: enabled, not already tried this 1P
        request, and the car is genuinely still charging on three phases while
        1 phase was requested."""
        if not self.cfg.phase_recovery_enabled:
            return False
        if self.recovery_active or self._downshift_attempted or data is None:
            return False
        if not data.vehicle_connected or not data.charging:
            return False
        return data.phase_switch_raw == 1 or self._measured_three_phase(data)

    def _start_recovery(self, direction: str = "up") -> None:
        if self.recovery_active:
            return
        self._recovery_direction = direction
        self._recovery_task = asyncio.create_task(self._recovery_sequence(direction))
        self._record(
            "recovery_attempt",
            f"phase {'downshift' if direction == 'down' else 'recovery'} started",
        )
        escalated = getattr(self.coordinator, "note_fix_escalated", None)
        if escalated is not None:
            escalated()

    def _cancel_recovery(self) -> None:
        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._buffer_commands = False

    def _set_recovery(self, status: str, remaining_s: int = 0) -> None:
        self._recovery_status = status
        self._recovery_remaining_s = max(0, remaining_s)
        self.coordinator.async_update_listeners()

    # -- sequence -------------------------------------------------------------
    async def _recovery_sequence(self, direction: str = "up") -> None:
        observe_s = self.cfg.phase_recovery_observe
        dwell_s = self.cfg.phase_recovery_dwell
        want = "1-phase" if direction == "down" else "3-phase"
        have = "3-phase" if direction == "down" else "1-phase"
        try:
            # OBSERVE: 405 is already written; watch whether the car goes to the
            # wanted phases on its own (cooperative cars / plug-in) before we
            # disrupt anything.
            _LOGGER.info("phase recovery: observing up to %ss for real %s", observe_s, want)
            terminal = await self._observe_phase(observe_s, direction)
            if terminal is not None:
                self._set_recovery(terminal)
                return

            # ESCALATE: the proven fix - a long pause so the car re-negotiates.
            if direction == "down":
                self._downshift_attempted = True  # at most one escalation per 1P request
            else:
                self._recovery_attempted = True   # at most one escalation per 3P request
            self._buffer_commands = True
            _LOGGER.info("phase recovery: still %s, forcing a %ss pause at 0 A", have, dwell_s)
            await self.coordinator.client.write_register(R.SET_CURRENT_A, 0)
            if not await self._dwell(dwell_s):
                _LOGGER.info("phase recovery: aborted (car disconnected during pause)")
                self._set_recovery(RECOVERY_ABORTED)
                return

            # No second 405 write: the register already holds the wanted phases.
            # Only the pause matters - the car re-reads the phase on its fresh
            # handshake.
            self._set_recovery(RECOVERY_RESUMING, PHASE_RECOVERY_SETTLE_S)
            await asyncio.sleep(PHASE_RECOVERY_SETTLE_S)
            await self._recovery_resume()
            self._set_recovery(RECOVERY_COMPLETE)
            _LOGGER.info("phase recovery: complete, charging resumed")
        except asyncio.CancelledError:
            self._set_recovery(RECOVERY_ABORTED)
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("phase recovery failed: %s", err)
            self._set_recovery(RECOVERY_ABORTED)
        finally:
            if self._recovery_status in (RECOVERY_COMPLETE, RECOVERY_ABORTED):
                self._last_recovery_at = datetime.now(timezone.utc)
                self._last_recovery_result = self._recovery_status
            self._buffer_commands = False
            self._recovery_remaining_s = 0
            self.coordinator.async_update_listeners()
            await self.coordinator.async_request_refresh()

    async def _observe_phase(self, observe_s: int, direction: str = "up") -> str | None:
        """Return a terminal status if we should stop, or None to escalate."""
        down = direction == "down"
        deadline = monotonic() + observe_s
        while True:
            self._set_recovery(RECOVERY_OBSERVING, int(ceil(deadline - monotonic())))
            data = self.coordinator.data
            if data is None or not data.vehicle_connected:
                return RECOVERY_ABORTED
            if down:
                if not data.charging or self._measured_single_phase(data):
                    return RECOVERY_COMPLETE  # went 1p on its own
            elif not data.charging or self._measured_three_phase(data):
                return RECOVERY_COMPLETE  # went 3p on its own
            if monotonic() >= deadline:
                break
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()
        data = self.coordinator.data
        if data is None or not data.vehicle_connected:
            return RECOVERY_ABORTED
        if down:
            if not data.charging or self._measured_single_phase(data):
                return RECOVERY_COMPLETE
            if not self._measured_three_phase(data):
                return RECOVERY_COMPLETE  # ambiguous reading -> don't disrupt
            return None  # confirmed still three-phase -> escalate
        if not data.charging or self._measured_three_phase(data):
            return RECOVERY_COMPLETE
        if not self._measured_single_phase(data):
            return RECOVERY_COMPLETE  # ambiguous reading -> don't disrupt
        return None  # confirmed still single-phase -> escalate

    async def _dwell(self, dwell_s: int) -> bool:
        """Hold 0 A for the dwell. Return False if the car unplugs meanwhile."""
        deadline = monotonic() + dwell_s
        while monotonic() < deadline:
            self._set_recovery(RECOVERY_DWELLING, int(ceil(deadline - monotonic())))
            await asyncio.sleep(1)
            data = self.coordinator.data
            if data is not None and not data.vehicle_connected:
                return False
        return True

    async def _recovery_resume(self) -> None:
        if self.is_external:
            # Last intent wins. A stop/disable during the pause is respected.
            if self._enabled_intent is False:
                value = 0
            elif self._current_intent is not None:
                value = self._current_intent
            else:
                value = self._ext_resume_current
            value = max(0, min(ABS_MAX_CURRENT_A, int(value)))
            if value > 0:
                self._ext_resume_current = value
            await self.coordinator.client.write_register(R.SET_CURRENT_A, value)
        else:
            # Internal: let the control loop write the freshly computed setpoint
            # for the new phase config on the next cycle.
            self._last_setpoint = None
