"""Mammotion Lawn Mower."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from datetime import time
from time import monotonic
from typing import Any, cast

import betterproto2
import voluptuous as vol
from homeassistant.components.lawn_mower import DOMAIN as LAWN_MOWER_DOMAIN
from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import service
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymammotion.data.model.report_info import DeviceData, ReportData
from pymammotion.utility.constant.device_constant import PosType, WorkMode
from pymammotion.utility.device_type import DeviceType

from . import MammotionConfigEntry
from .const import DOMAIN, LOGGER
from .control_state import MowerControlState
from .coordinator import MammotionReportUpdateCoordinator
from .entity import MammotionBaseEntity

SERVICE_START_MOWING = "start_mow"
SERVICE_CANCEL_JOB = "cancel_job"
SERVICE_START_STOP_BLADES = "start_stop_blades"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"
SERVICE_RESET_BLADE_TIME = "reset_blade_time"
SERVICE_SET_BLADE_WARNING_TIME = "set_blade_warning_time"

START_CONFIRM_TIMEOUT = 240
START_PREEMPT_CONFIRM_TIMEOUT = START_CONFIRM_TIMEOUT


class _CommandPreempted(Exception):
    """A safety action superseded a start before it was dispatched."""


@dataclass(frozen=True, slots=True)
class _StartPreemption:
    """State captured while a safety action preempts a pending start."""

    pending: bool
    may_be_active: bool


START_MOW_SCHEMA = {
    vol.Optional("modify", default=False): cv.boolean,
    vol.Optional("plan_only", default=False): cv.boolean,
    vol.Optional("is_mow", default=True): cv.boolean,
    vol.Optional("is_dump", default=True): cv.boolean,
    vol.Optional("is_edge", default=False): cv.boolean,
    vol.Optional("collect_grass_frequency", default=10): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=100)
    ),
    vol.Optional("border_mode", default=1): vol.All(vol.Coerce(int), vol.In([0, 1])),
    vol.Optional("job_version", default=0): vol.Coerce(int),
    vol.Optional("job_id", default=0): vol.Coerce(int),
    vol.Optional("speed", default=0.3): vol.All(
        vol.Coerce(float), vol.Range(min=0.2, max=1.2)
    ),
    vol.Optional("ultra_wave", default=2): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 10, 11])
    ),
    vol.Optional("channel_mode", default=0): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3])
    ),
    vol.Optional("channel_width", default=25): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=35)
    ),
    vol.Optional("rain_tactics", default=1): vol.All(vol.Coerce(int), vol.In([0, 1])),
    vol.Optional("blade_height", default=25): vol.All(
        vol.Coerce(int), vol.Range(min=15, max=100)
    ),
    vol.Optional("toward", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=-180, max=180)
    ),
    vol.Optional("toward_included_angle", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=-180, max=180)
    ),
    vol.Optional("toward_mode", default=0): vol.All(vol.Coerce(int), vol.In([0, 1, 2])),
    vol.Optional("mowing_laps", default=1): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
    ),
    vol.Optional("obstacle_laps", default=1): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
    ),
    vol.Optional("start_progress", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=100)
    ),
    vol.Optional("areas", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
}

START_STOP_BLADES_SCHEMA = {
    vol.Required("start_stop", default=True): cv.boolean,
    vol.Optional("blade_height", default=30): vol.All(
        vol.Coerce(int), vol.Range(min=15, max=100)
    ),
}

SET_NON_WORK_HOURS_SCHEMA = {
    vol.Required("start_time"): cv.time,
    vol.Required("end_time"): cv.time,
}

SET_BLADE_WARNING_TIME_SCHEMA = {
    vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=1, max=9999)),
}


def get_entity_attribute(
    hass: HomeAssistant, entity_id: str, attribute_name: str
) -> str | None:
    """Return a named attribute from a HA entity state, or None if unavailable."""
    # Get the state object of the entity
    entity = hass.states.get(entity_id)

    # Check if the entity exists and has attributes
    if entity and attribute_name in entity.attributes:
        # Return the specific attribute
        return cast(str | None, entity.attributes.get(attribute_name))
    # Return None if the entity or attribute does not exist
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Mammotion Lawn Mower config entry."""
    mammotion_devices = entry.runtime_data.mowers

    entities = [
        MammotionLawnMowerEntity(mower.reporting_coordinator)
        for mower in mammotion_devices
    ]

    async_add_entities(entities)

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_START_MOWING,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=START_MOW_SCHEMA,
        func="async_start_mowing",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CANCEL_JOB,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=None,
        func="async_cancel",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_START_STOP_BLADES,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=START_STOP_BLADES_SCHEMA,
        func="async_start_stop_blades",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_NON_WORK_HOURS,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=SET_NON_WORK_HOURS_SCHEMA,
        func="async_set_non_work_hours",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESET_BLADE_TIME,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=None,
        func="async_reset_blade_time",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_BLADE_WARNING_TIME,
        entity_domain=LAWN_MOWER_DOMAIN,
        schema=SET_BLADE_WARNING_TIME_SCHEMA,
        func="async_set_blade_warning_time",
    )


class MammotionLawnMowerEntity(MammotionBaseEntity, LawnMowerEntity):  # type: ignore[misc]
    """Representation of a Mammotion Lawn Mower."""

    def __init__(self, coordinator: MammotionReportUpdateCoordinator) -> None:
        """Initialize the Lawn Mower."""
        super().__init__(coordinator, "mower")
        self._attr_name = None  # main feature of device
        self._command_lock = asyncio.Lock()
        self._active_start_cancel: asyncio.Event | None = None
        self._start_cancel_events: set[asyncio.Event] = set()
        self._start_dispatching = False
        self._start_dispatched = False

    def _preempt_pending_start(self) -> _StartPreemption:
        """Wake pending start operations before acquiring the command lock."""
        preemption = _StartPreemption(
            pending=bool(self._start_cancel_events),
            may_be_active=self._start_dispatching or self._start_dispatched,
        )
        for cancel_event in self._start_cancel_events:
            cancel_event.set()
        return preemption

    async def _async_wait_unless_preempted(self, awaitable: Any) -> Any:
        """Await start work unless a safety action supersedes its planning."""
        cancel_event = self._active_start_cancel
        if cancel_event is None:
            return await awaitable
        if cancel_event.is_set() and not (
            self._start_dispatching or self._start_dispatched
        ):
            awaitable.close()
            raise _CommandPreempted

        work_task = asyncio.create_task(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done:
                return await work_task
            if not (self._start_dispatching or self._start_dispatched):
                work_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work_task
                raise _CommandPreempted
            return await work_task
        except asyncio.CancelledError:
            cancel_event.set()
            if not self._start_dispatching:
                work_task.cancel()
            await asyncio.shield(asyncio.gather(work_task, return_exceptions=True))
            raise
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    async def _async_wait_for_start_report(self, *, since: int, timeout: float) -> bool:
        """Wait for start telemetry while allowing a safety action to take over."""
        cancel_event = self._active_start_cancel
        if cancel_event is None:
            return await self.coordinator.async_wait_for_report_data(
                since=since,
                timeout=timeout,
            )
        if cancel_event.is_set():
            raise _CommandPreempted

        report_task = asyncio.create_task(
            self.coordinator.async_wait_for_report_data(
                since=since,
                timeout=timeout,
            )
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {report_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if report_task in done:
                return await report_task
            report_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await report_task
            raise _CommandPreempted
        finally:
            report_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(
                report_task,
                cancel_task,
                return_exceptions=True,
            )

    @property
    def rpt_dev_status(self) -> DeviceData:
        """Return the device status."""
        return self.coordinator.data.report_data.dev

    @property
    def report_data(self) -> ReportData:
        """Return the report data."""
        return self.coordinator.data.report_data

    @property
    def control_state(self) -> MowerControlState:
        """Return command availability derived from current reported state."""
        return MowerControlState(
            mode=self.rpt_dev_status.sys_status,
            command_ready=self.coordinator.command_ready,
            on_charger=(
                self.rpt_dev_status.charge_state != 0
                or self.coordinator.data.location.position_type
                == PosType.CHARGE_ON.value
            ),
            breakpoint_info=self.report_data.work.bp_info,
            selected_area_count=len(self.coordinator.operation_settings.areas),
        )

    @property
    def supported_features(self) -> LawnMowerEntityFeature:
        """Expose only commands valid for the latest reported state."""
        features = LawnMowerEntityFeature(0)
        state = self.control_state
        if state.can_start:
            features |= LawnMowerEntityFeature.START_MOWING
        if state.can_pause:
            features |= LawnMowerEntityFeature.PAUSE
        if state.can_dock:
            features |= LawnMowerEntityFeature.DOCK
        if self._start_cancel_events:
            features |= LawnMowerEntityFeature.PAUSE | LawnMowerEntityFeature.DOCK
        return features

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Return the state of the mower."""

        charge_state = self.rpt_dev_status.charge_state
        mode = self.rpt_dev_status.sys_status
        if mode is None:
            return None

        LOGGER.debug("activity mode %s", mode)
        if mode in (WorkMode.MODE_PAUSE, WorkMode.MODE_CHARGING_PAUSE) or (
            mode == WorkMode.MODE_READY and charge_state == 0
        ):
            return LawnMowerActivity.PAUSED
        if mode == WorkMode.MODE_WORKING:
            return LawnMowerActivity.MOWING
        if mode == WorkMode.MODE_RETURNING:
            return LawnMowerActivity.RETURNING
        if mode == WorkMode.MODE_LOCK:
            return LawnMowerActivity.ERROR
        if mode == WorkMode.MODE_READY and charge_state != 0:
            return LawnMowerActivity.DOCKED
        return None

    async def _async_task_control(
        self,
        command: str,
        *,
        action: int,
        expected_modes: set[int],
        translation_key: str,
        timeout: float,
        success_predicate: Callable[[], bool] | None = None,
    ) -> None:
        """Send one task command and verify its ACK and reported result."""
        await self.coordinator.async_start_report_stream(
            duration_ms=int(timeout * 1000)
        )
        report_token = self.coordinator.report_data_token
        is_start_command = command in {"start_job", "resume_execute_task"}
        if is_start_command:
            if (
                self._active_start_cancel is not None
                and self._active_start_cancel.is_set()
            ):
                raise _CommandPreempted
            self._start_dispatching = True
        try:
            response = await self.coordinator.async_send_and_wait(
                command,
                "todev_taskctrl_ack",
                preempt_reads=True,
            )
        finally:
            if is_start_command:
                self._start_dispatching = False
        if response is not None:
            try:
                field, ack = betterproto2.which_one_of(response.nav, "SubNavMsg")
            except AttributeError, TypeError, ValueError:
                field, ack = None, None
            if field == "todev_taskctrl_ack" and (
                int(ack.action) != action or int(ack.result) != 0
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=translation_key,
                )
        if is_start_command:
            self._start_dispatched = True
            if (
                self._active_start_cancel is not None
                and self._active_start_cancel.is_set()
            ):
                raise _CommandPreempted

        deadline = monotonic() + timeout
        while True:
            latest_token = self.coordinator.report_data_token
            if (
                latest_token != report_token
                and self.rpt_dev_status.sys_status in expected_modes
                and (success_predicate is None or success_predicate())
            ):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            report_token = latest_token
            if is_start_command:
                report_received = await self._async_wait_for_start_report(
                    since=report_token,
                    timeout=remaining,
                )
            else:
                report_received = await self.coordinator.async_wait_for_report_data(
                    since=report_token,
                    timeout=remaining,
                )
            if not report_received:
                break

        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=translation_key,
        )

    async def _async_require_fresh_state(self) -> None:
        """Require command transport and recent device telemetry."""
        if not await self.coordinator.async_ensure_fresh_report_data():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_not_ready",
            )

    async def async_start_mowing(self, **kwargs: Any) -> None:
        """Start or resume a mowing task."""
        cancel_event = asyncio.Event()
        self._start_cancel_events.add(cancel_event)
        try:
            async with self._command_lock:
                self._active_start_cancel = cancel_event
                self._start_dispatching = False
                self._start_dispatched = False
                await self._async_wait_unless_preempted(
                    self._async_start_transaction(**kwargs)
                )
        except _CommandPreempted:
            return
        finally:
            self._start_cancel_events.discard(cancel_event)
            if self._active_start_cancel is cancel_event:
                self._active_start_cancel = None
                self._start_dispatching = False
                self._start_dispatched = False

    async def _async_start_transaction(self, **kwargs: Any) -> None:
        """Refresh state and start while safety actions can preempt the wait."""
        await self._async_require_fresh_state()
        await self._async_start_mowing_locked(**kwargs)

    async def _async_start_mowing_locked(self, **kwargs: Any) -> None:
        """Start or resume a mowing task while commands are serialized."""
        explicit_route = bool(kwargs)
        entity_ids = kwargs.pop("areas", None)
        modify_plan = kwargs.pop("modify", False)
        plan_only = kwargs.pop("plan_only", False)
        operational_settings = copy(self.coordinator.operation_settings)

        if entity_ids is not None:
            operational_settings.areas = list(
                dict.fromkeys(
                    int(entity_hash)
                    for entity_id in entity_ids
                    if (
                        entity_hash := get_entity_attribute(
                            self.hass, entity_id, "hash"
                        )
                    )
                    is not None
                )
            )
        for key, value in kwargs.items():
            setattr(operational_settings, key, value)
        if DeviceType.is_yuka(self.coordinator.device_name):
            operational_settings.blade_height = -10

        mode = self.rpt_dev_status.sys_status
        if modify_plan:
            if mode != WorkMode.MODE_WORKING:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_not_ready",
                )
            if not await self.coordinator.async_modify_plan_route(operational_settings):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="start_failed",
                )
            return

        try:
            if explicit_route and self.control_state.can_cancel:
                await self._async_cancel_locked(refresh_state=False)
                await self._async_require_fresh_state()
                mode = self.rpt_dev_status.sys_status

            if not explicit_route and mode == WorkMode.MODE_RETURNING:
                await self._async_task_control(
                    "cancel_return_to_dock",
                    action=12,
                    expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                    translation_key="dock_cancel_failed",
                    timeout=30,
                )
                mode = self.rpt_dev_status.sys_status

            breakpoint_info = self.report_data.work.bp_info
            if mode in (WorkMode.MODE_PAUSE, WorkMode.MODE_CHARGING_PAUSE):
                if breakpoint_info == 0:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="device_not_ready",
                    )
                if not await self.coordinator.async_query_plan_route(
                    preempt_reads=True
                ):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="resume_failed",
                    )
                if not plan_only:
                    await self._async_task_control(
                        "resume_execute_task",
                        action=3,
                        expected_modes={WorkMode.MODE_WORKING},
                        translation_key="resume_failed",
                        timeout=60,
                    )
                return

            if mode not in (WorkMode.MODE_READY, WorkMode.MODE_INITIALIZATION):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_not_ready",
                )

            if breakpoint_info != 0:
                if not await self.coordinator.async_query_plan_route(
                    preempt_reads=True
                ):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="start_failed",
                    )
            else:
                if not operational_settings.areas:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="device_not_ready",
                    )
                if not await self.coordinator.async_plan_route(
                    operational_settings,
                    preempt_reads=True,
                ):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="start_failed",
                    )

            if not plan_only:
                await self._async_task_control(
                    "start_job",
                    action=1,
                    expected_modes={WorkMode.MODE_WORKING},
                    translation_key="start_failed",
                    timeout=START_CONFIRM_TIMEOUT,
                )
        finally:
            await self.coordinator.async_request_report_snapshot()

    async def async_dock(self) -> None:
        """Start return-to-dock when the reported state permits it."""
        preemption = self._preempt_pending_start()
        async with self._command_lock:
            await self._async_require_fresh_state()
            state = self.control_state
            if not (state.can_dock or preemption.pending):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_not_ready",
                )
            try:
                if (
                    self.rpt_dev_status.sys_status == WorkMode.MODE_WORKING
                    or preemption.may_be_active
                ):
                    await self._async_task_control(
                        "pause_execute_task",
                        action=2,
                        expected_modes={WorkMode.MODE_PAUSE},
                        translation_key="pause_failed",
                        timeout=(
                            START_PREEMPT_CONFIRM_TIMEOUT
                            if preemption.may_be_active
                            else 20
                        ),
                    )
                state = self.control_state
                if state.on_charger:
                    if state.mode in (
                        WorkMode.MODE_PAUSE,
                        WorkMode.MODE_CHARGING_PAUSE,
                    ) and state.can_cancel:
                        await self._async_cancel_locked(refresh_state=False)
                    return
                if state.mode == WorkMode.MODE_RETURNING:
                    return
                await self._async_task_control(
                    "return_to_dock",
                    action=5,
                    expected_modes={WorkMode.MODE_RETURNING},
                    translation_key="dock_failed",
                    timeout=30,
                )
            finally:
                await self.coordinator.async_request_report_snapshot()

    async def async_pause(self) -> None:
        """Pause active work or cancel a return to dock."""
        preemption = self._preempt_pending_start()
        async with self._command_lock:
            await self._async_require_fresh_state()
            if not (self.control_state.can_pause or preemption.pending):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_not_ready",
                )
            try:
                if self.rpt_dev_status.sys_status == WorkMode.MODE_RETURNING:
                    await self._async_task_control(
                        "cancel_return_to_dock",
                        action=12,
                        expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                        translation_key="dock_cancel_failed",
                        timeout=30,
                    )
                elif (
                    self.rpt_dev_status.sys_status == WorkMode.MODE_WORKING
                    or preemption.may_be_active
                ):
                    await self._async_task_control(
                        "pause_execute_task",
                        action=2,
                        expected_modes={WorkMode.MODE_PAUSE},
                        translation_key="pause_failed",
                        timeout=(
                            START_PREEMPT_CONFIRM_TIMEOUT
                            if preemption.may_be_active
                            else 20
                        ),
                    )
            finally:
                await self.coordinator.async_request_report_snapshot()

    async def async_cancel(self) -> None:
        """Cancel the active task or retained breakpoint."""
        preemption = self._preempt_pending_start()
        async with self._command_lock:
            await self._async_require_fresh_state()
            await self._async_cancel_locked(
                refresh_state=False,
                start_pending=preemption.pending,
                start_may_be_active=preemption.may_be_active,
            )

    async def _async_cancel_locked(
        self,
        *,
        refresh_state: bool = True,
        start_pending: bool = False,
        start_may_be_active: bool = False,
    ) -> None:
        """Cancel a task while the per-mower command lock is held."""
        if refresh_state:
            await self._async_require_fresh_state()
        if not (self.control_state.can_cancel or start_pending or start_may_be_active):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_not_ready",
            )

        try:
            mode = self.rpt_dev_status.sys_status
            if mode == WorkMode.MODE_WORKING or start_may_be_active:
                await self._async_task_control(
                    "pause_execute_task",
                    action=2,
                    expected_modes={WorkMode.MODE_PAUSE},
                    translation_key="pause_failed",
                    timeout=(
                        START_PREEMPT_CONFIRM_TIMEOUT if start_may_be_active else 20
                    ),
                )
            elif mode == WorkMode.MODE_RETURNING:
                await self._async_task_control(
                    "cancel_return_to_dock",
                    action=12,
                    expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                    translation_key="dock_cancel_failed",
                    timeout=30,
                )

            mode = self.rpt_dev_status.sys_status
            if mode in (WorkMode.MODE_PAUSE, WorkMode.MODE_CHARGING_PAUSE) or (
                mode == WorkMode.MODE_READY and self.report_data.work.bp_info != 0
            ):
                await self._async_task_control(
                    "cancel_job",
                    action=4,
                    expected_modes={WorkMode.MODE_READY},
                    translation_key="command_failed",
                    timeout=30,
                    success_predicate=lambda: self.report_data.work.bp_info == 0,
                )
        finally:
            await self.coordinator.async_request_report_snapshot()

    async def async_start_stop_blades(self, **kwargs: Any) -> None:
        """Start/Stop Blades."""
        await self.coordinator.async_start_stop_blades(**kwargs)

    async def async_set_non_work_hours(self, **kwargs: Any) -> None:
        """Set Non Work Hours."""
        start_time: time = kwargs["start_time"]
        end_time: time = kwargs["end_time"]

        await self.coordinator.async_set_non_work_hours(
            start_time=start_time.strftime("%H:%M"), end_time=end_time.strftime("%H:%M")
        )

    async def async_reset_blade_time(self) -> None:
        """Reset blade used time to zero."""
        if DeviceType.is_luba1(self.coordinator.device_name):
            return
        await self.coordinator.async_reset_blade_time()

    async def async_set_blade_warning_time(self, hours: int) -> None:
        """Set blade replacement warning threshold in hours."""
        if DeviceType.is_luba1(self.coordinator.device_name):
            return
        await self.coordinator.async_set_blade_warning_time(hours=hours)

    async def async_added_to_hass(self) -> None:
        """Register callbacks and verify device linkage after HA setup."""
        await super().async_added_to_hass()

        # Ensure the entity is actually linked to a device
        if not self.coordinator.device_name:
            return

        device_registry = dr.async_get(self.hass)

        device = device_registry.async_get_device(
            identifiers={(DOMAIN, self.coordinator.device_name)}
        )

        if device:
            for conn_type, value in device.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    self.coordinator.data.mower_state.wifi_mac = value
                elif conn_type == dr.CONNECTION_BLUETOOTH:
                    self.coordinator.data.mower_state.ble_mac = value
