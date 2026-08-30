"""Mammotion Lawn Mower."""

from __future__ import annotations

import asyncio
import contextlib
from copy import copy
from datetime import UTC, datetime, time, timedelta
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
from .const import COMMAND_EXCEPTIONS, DOMAIN, LOGGER
from .coordinator import MammotionReportUpdateCoordinator
from .entity import MammotionBaseEntity
from .errors import classify_mammotion_error, get_mammotion_error_details

SERVICE_START_MOWING = "start_mow"
SERVICE_CANCEL_JOB = "cancel_job"
SERVICE_START_STOP_BLADES = "start_stop_blades"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"
SERVICE_RESET_BLADE_TIME = "reset_blade_time"
SERVICE_SET_BLADE_WARNING_TIME = "set_blade_warning_time"


class _CommandPreempted(Exception):
    """Raised internally when a safety action supersedes a pending start."""

START_MOW_SCHEMA = {
    vol.Optional("modify"): cv.boolean,
    vol.Optional("plan_only"): cv.boolean,
    vol.Optional("is_mow"): cv.boolean,
    vol.Optional("is_dump"): cv.boolean,
    vol.Optional("is_edge"): cv.boolean,
    vol.Optional("collect_grass_frequency"): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=100)
    ),
    vol.Optional("border_mode"): vol.All(vol.Coerce(int), vol.In([0, 1])),
    vol.Optional("job_version"): vol.Coerce(int),
    vol.Optional("job_id"): vol.Coerce(int),
    vol.Optional("speed"): vol.All(
        vol.Coerce(float), vol.Range(min=0.2, max=1.2)
    ),
    vol.Optional("ultra_wave"): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 10, 11])
    ),
    vol.Optional("channel_mode"): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3])
    ),
    vol.Optional("channel_width"): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=35)
    ),
    vol.Optional("rain_tactics"): vol.All(vol.Coerce(int), vol.In([0, 1])),
    vol.Optional("blade_height"): vol.All(
        vol.Coerce(int), vol.Range(min=15, max=100)
    ),
    vol.Optional("toward"): vol.All(
        vol.Coerce(int), vol.Range(min=-180, max=180)
    ),
    vol.Optional("toward_included_angle"): vol.All(
        vol.Coerce(int), vol.Range(min=-180, max=180)
    ),
    vol.Optional("toward_mode"): vol.All(vol.Coerce(int), vol.In([0, 1, 2])),
    vol.Optional("mowing_laps"): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
    ),
    vol.Optional("obstacle_laps"): vol.All(
        vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
    ),
    vol.Optional("start_progress"): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=100)
    ),
    vol.Optional("areas"): vol.All(cv.ensure_list, [cv.entity_id]),
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

    _attr_supported_features = (
        LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.START_MOWING
    )

    def __init__(self, coordinator: MammotionReportUpdateCoordinator) -> None:
        """Initialize the Lawn Mower."""
        super().__init__(coordinator, "mower")
        self._attr_name = None  # main feature of device
        self._command_lock = asyncio.Lock()
        self._active_start_cancel: asyncio.Event | None = None
        self._start_cancel_events: set[asyncio.Event] = set()
        self._start_dispatched = False

    def _preempt_active_start(self) -> None:
        """Wake a start operation so a safety action can acquire the lock."""
        for cancel_event in self._start_cancel_events:
            cancel_event.set()

    async def _async_wait_unless_preempted(self, awaitable: Any) -> Any:
        """Await work unless the active start is superseded by a safety action."""
        cancel_event = self._active_start_cancel
        if cancel_event is None:
            return await awaitable
        if cancel_event.is_set() and not self._start_dispatched:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise _CommandPreempted

        work_task = asyncio.create_task(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if work_task in done:
                return await work_task
            if cancel_task in done and not self._start_dispatched:
                work_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work_task
                raise _CommandPreempted
            return await work_task
        except asyncio.CancelledError:
            work_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work_task
            raise
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    @property
    def rpt_dev_status(self) -> DeviceData:
        """Return the device status."""
        return self.coordinator.data.report_data.dev

    @property
    def report_data(self) -> ReportData:
        """Return the report data."""
        return self.coordinator.data.report_data

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Return the state of the mower."""

        charge_state = self.rpt_dev_status.charge_state
        mode = self.rpt_dev_status.sys_status
        if mode is None:
            return None

        LOGGER.debug("activity mode %s", mode)
        position_reports_docked = (
            self.coordinator.data.location.position_type == PosType.CHARGE_ON.value
        )
        if mode == WorkMode.MODE_CHARGING_PAUSE:
            # Charging pause belongs to an unfinished mowing task.
            return LawnMowerActivity.PAUSED
        if mode == WorkMode.MODE_PAUSE:
            if position_reports_docked and charge_state != 0:
                return LawnMowerActivity.DOCKED
            return LawnMowerActivity.PAUSED
        if mode == WorkMode.MODE_READY and charge_state == 0:
            if position_reports_docked:
                return LawnMowerActivity.DOCKED
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

    async def _async_start_job_and_verify(self, trans_key: str) -> None:
        """Start the planned job and verify the mower actually begins work."""
        await self._async_task_control(
            "start_job",
            action=1,
            expected_modes={WorkMode.MODE_WORKING},
            trans_key=trans_key,
            timeout=90,
        )

    async def _async_task_control(
        self,
        command: str,
        *,
        action: int,
        expected_modes: set[WorkMode],
        trans_key: str,
        timeout: float = 15.0,
    ) -> None:
        """Send one task command and verify its ACK and reported final state."""
        await self.coordinator.async_start_report_stream(
            duration_ms=int(timeout * 1000)
        )
        report_token = self.coordinator.report_data_token
        before_send = None
        if command in {"start_job", "resume_execute_task"}:
            # Once the outbound call is entered, the mower may begin later even if
            # its ACK or current report still says READY. Keep the transaction
            # accountable until fresh telemetry arrives; a queued safety action
            # then runs.

            def _cancel_or_mark_dispatched() -> None:
                if self._start_dispatched:
                    return
                if (
                    self._active_start_cancel is not None
                    and self._active_start_cancel.is_set()
                ):
                    raise _CommandPreempted
                self._start_dispatched = True

            before_send = _cancel_or_mark_dispatched
        response = await self.coordinator.async_send_and_wait(
            command,
            "todev_taskctrl_ack",
            retry_on_timeout=False,
            before_send=before_send,
        )
        deadline = monotonic() + timeout
        ack = None
        if response is not None:
            try:
                field, ack = betterproto2.which_one_of(response.nav, "SubNavMsg")
            except (AttributeError, TypeError, ValueError):
                field = None
            if field != "todev_taskctrl_ack":
                ack = None
        if ack is not None and (int(ack.action) != action or int(ack.result) != 0):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=trans_key
            )

        while True:
            latest_token = self.coordinator.report_data_token
            if (
                latest_token != report_token
                and self.rpt_dev_status.sys_status in expected_modes
            ):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            report_token = latest_token
            if not await self.coordinator.async_wait_for_report_data(
                since=report_token, timeout=remaining
            ):
                break

        await self.coordinator.async_request_report_snapshot()
        error = get_mammotion_error_details(
            self.coordinator.data, self.hass.config.language
        )
        if (
            error is not None
            and error.occurred_at is not None
            and error.occurred_at >= datetime.now(UTC) - timedelta(minutes=10)
        ):
            classification = classify_mammotion_error(
                self.coordinator.data, error, command_rejected=True
            )
            raise HomeAssistantError(
                f"Mammotion {classification.value} error {error.code}: {error.message}"
            )
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=trans_key)

    async def async_start_mowing(self, **kwargs: Any) -> None:
        """Start mowing."""
        cancel_event = asyncio.Event()
        self._start_cancel_events.add(cancel_event)
        try:
            await self.coordinator.async_interrupt_sagas()
            async with self._command_lock:
                self._active_start_cancel = cancel_event
                self._start_dispatched = False
                await self._async_wait_unless_preempted(
                    self._async_start_mowing_locked(**kwargs)
                )
        except _CommandPreempted:
            return
        finally:
            self._start_cancel_events.discard(cancel_event)
            if self._active_start_cancel is cancel_event:
                self._active_start_cancel = None
                self._start_dispatched = False

    async def _async_start_mowing_locked(self, **kwargs: Any) -> None:
        """Start mowing while holding the per-mower command lock."""
        trans_key = "pause_failed"

        if not await self.coordinator.async_ensure_fresh_report_data():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )

        explicit_route = bool(kwargs)
        if kwargs:
            entity_ids = kwargs.pop("areas", None)
            modify_plan = kwargs.pop("modify", False)
            plan_only = kwargs.pop("plan_only", False)

            # Merge onto coordinator's restored settings so UI-configured values
            # (speed, blade_height, etc.) are preserved when not explicitly provided.
            operational_settings = copy(self.coordinator.operation_settings)
            if entity_ids is not None:
                attributes = []
                for entity_id in entity_ids:
                    entity_hash = get_entity_attribute(self.hass, entity_id, "hash")
                    if entity_hash is not None:
                        attributes.append(int(entity_hash))
                operational_settings.areas = list(dict.fromkeys(attributes))
            for key, value in kwargs.items():
                setattr(operational_settings, key, value)
            if DeviceType.is_yuka(self.coordinator.device_name):
                operational_settings.blade_height = -10
            LOGGER.debug(kwargs)
            LOGGER.debug(operational_settings)
        else:
            operational_settings = self.coordinator.operation_settings
            modify_plan = False
            plan_only = False

        # check if job in progress
        #
        mode = self.rpt_dev_status.sys_status
        breakpoint_info = self.report_data.work.bp_info
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )

        if mode in (
            WorkMode.MODE_PAUSE,
            WorkMode.MODE_READY,
            WorkMode.MODE_RETURNING,
            WorkMode.MODE_WORKING,
            WorkMode.MODE_INITIALIZATION,
        ):
            try:
                if modify_plan:
                    await self.coordinator.async_modify_plan_route(operational_settings)
                    return

                if explicit_route:
                    await self._async_cancel_locked(refresh_state=False)
                    await self.coordinator.async_request_report_snapshot()
                    for _ in range(5):
                        mode = self.rpt_dev_status.sys_status
                        if mode != WorkMode.MODE_PAUSE:
                            break
                        await asyncio.sleep(1)
                        await self.coordinator.async_request_report_snapshot()
                    mode = self.rpt_dev_status.sys_status
                    breakpoint_info = 0
                    if (
                        mode == WorkMode.MODE_PAUSE
                        and self.rpt_dev_status.charge_state != 0
                    ):
                        mode = WorkMode.MODE_READY

                if mode == WorkMode.MODE_RETURNING:
                    trans_key = "dock_cancel_failed"
                    await self._async_task_control(
                        "cancel_return_to_dock",
                        action=12,
                        expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                        trans_key=trans_key,
                        timeout=30,
                    )
                    mode = self.rpt_dev_status.sys_status
                if mode == WorkMode.MODE_PAUSE:
                    trans_key = "resume_failed"
                    if breakpoint_info != 0:
                        response = await self.coordinator.async_send_and_wait(
                            "query_generate_route_information", "bidire_reqconver_path"
                        )
                        if not self.coordinator.sync_operation_settings_from_route_response(
                            response
                        ):
                            raise HomeAssistantError(
                                translation_domain=DOMAIN, translation_key=trans_key
                            )
                        await self._async_task_control(
                            "resume_execute_task",
                            action=3,
                            expected_modes={WorkMode.MODE_WORKING},
                            trans_key=trans_key,
                            timeout=60,
                        )
                if mode in (WorkMode.MODE_READY, WorkMode.MODE_INITIALIZATION):
                    trans_key = "start_failed"
                    if breakpoint_info != 0:
                        response = await self.coordinator.async_send_and_wait(
                            "query_generate_route_information", "bidire_reqconver_path"
                        )
                        if not self.coordinator.sync_operation_settings_from_route_response(
                            response
                        ):
                            raise HomeAssistantError(
                                translation_domain=DOMAIN, translation_key=trans_key
                            )
                        if not plan_only:
                            await self._async_start_job_and_verify(trans_key)
                        return
                    if not await self.coordinator.async_plan_route(
                        operational_settings
                    ):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key=trans_key
                        )
                    if not plan_only:
                        await self._async_start_job_and_verify(trans_key)

            except COMMAND_EXCEPTIONS as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=trans_key
                ) from exc
            finally:
                await self.coordinator.async_request_report_snapshot()

    async def async_dock(self) -> None:
        """Start docking."""
        self._preempt_active_start()
        await self.coordinator.async_interrupt_sagas()
        async with self._command_lock:
            await self._async_dock_locked()

    async def _async_dock_locked(self) -> None:
        """Start docking while holding the per-mower command lock."""
        trans_key = "pause_failed"

        if not await self.coordinator.async_ensure_fresh_report_data():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )
        charge_state = self.rpt_dev_status.charge_state
        mode = self.rpt_dev_status.sys_status
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )

        if charge_state == 0 and mode in (
            WorkMode.MODE_WORKING,
            WorkMode.MODE_PAUSE,
            WorkMode.MODE_READY,
            WorkMode.MODE_RETURNING,
        ):
            try:
                if mode == WorkMode.MODE_WORKING:
                    trans_key = "pause_failed"
                    await self._async_task_control(
                        "pause_execute_task",
                        action=2,
                        expected_modes={WorkMode.MODE_PAUSE},
                        trans_key=trans_key,
                        timeout=20,
                    )

                if mode == WorkMode.MODE_RETURNING:
                    return
                trans_key = "dock_failed"
                await self._async_task_control(
                    "return_to_dock",
                    action=5,
                    expected_modes={WorkMode.MODE_RETURNING},
                    trans_key=trans_key,
                    timeout=30,
                )
            except COMMAND_EXCEPTIONS as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=trans_key
                ) from exc
            finally:
                await self.coordinator.async_request_report_snapshot()

    async def async_pause(self) -> None:
        """Pause mower."""
        self._preempt_active_start()
        await self.coordinator.async_interrupt_sagas()
        async with self._command_lock:
            await self._async_pause_locked()

    async def _async_pause_locked(self) -> None:
        """Pause the mower while holding the per-mower command lock."""
        trans_key = "pause_failed"

        if not await self.coordinator.async_ensure_fresh_report_data():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )
        mode = self.rpt_dev_status.sys_status
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )

        if mode in (
            WorkMode.MODE_WORKING,
            WorkMode.MODE_RETURNING,
        ):
            try:
                if mode == WorkMode.MODE_WORKING:
                    trans_key = "pause_failed"
                    await self._async_task_control(
                        "pause_execute_task",
                        action=2,
                        expected_modes={WorkMode.MODE_PAUSE},
                        trans_key=trans_key,
                        timeout=20,
                    )
                if mode == WorkMode.MODE_RETURNING:
                    trans_key = "dock_cancel_failed"
                    await self._async_task_control(
                        "cancel_return_to_dock",
                        action=12,
                        expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                        trans_key=trans_key,
                        timeout=30,
                    )
            except COMMAND_EXCEPTIONS as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=trans_key
                ) from exc
            finally:
                await self.coordinator.async_request_report_snapshot()

    async def async_cancel(self) -> None:
        """Cancel Job."""
        self._preempt_active_start()
        await self.coordinator.async_interrupt_sagas()
        async with self._command_lock:
            await self._async_cancel_locked()

    async def _async_cancel_locked(self, *, refresh_state: bool = True) -> None:
        """Cancel the job while holding the per-mower command lock."""
        trans_key = "pause_failed"

        if refresh_state and not await self.coordinator.async_ensure_fresh_report_data():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )
        mode = self.rpt_dev_status.sys_status
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_not_ready"
            )

        if mode in (
            WorkMode.MODE_PAUSE,
            WorkMode.MODE_CHARGING_PAUSE,
            WorkMode.MODE_WORKING,
            WorkMode.MODE_RETURNING,
        ):
            try:
                if mode not in (
                    WorkMode.MODE_PAUSE,
                    WorkMode.MODE_CHARGING_PAUSE,
                ):
                    if mode == WorkMode.MODE_WORKING:
                        trans_key = "pause_failed"
                        await self._async_task_control(
                            "pause_execute_task",
                            action=2,
                            expected_modes={WorkMode.MODE_PAUSE},
                            trans_key=trans_key,
                            timeout=20,
                        )
                    if mode == WorkMode.MODE_RETURNING:
                        trans_key = "dock_cancel_failed"
                        await self._async_task_control(
                            "cancel_return_to_dock",
                            action=12,
                            expected_modes={WorkMode.MODE_PAUSE, WorkMode.MODE_READY},
                            trans_key=trans_key,
                            timeout=30,
                        )
                    mode = self.rpt_dev_status.sys_status

                if mode in (
                    WorkMode.MODE_PAUSE,
                    WorkMode.MODE_CHARGING_PAUSE,
                ) or (
                    mode == WorkMode.MODE_READY
                    and self.report_data.work.bp_info != 0
                ):
                    trans_key = "command_failed"
                    await self._async_task_control(
                        "cancel_job",
                        action=4,
                        expected_modes={WorkMode.MODE_READY},
                        trans_key=trans_key,
                        timeout=30,
                    )

            except COMMAND_EXCEPTIONS as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=trans_key
                ) from exc
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
