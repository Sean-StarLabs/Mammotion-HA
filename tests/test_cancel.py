"""Tests for acknowledged mower cancellation."""

# The command transaction is intentionally tested through its private boundary.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from pymammotion.proto import LubaMsg, MctlNav, NavTaskCtrlAck
from pymammotion.utility.constant.device_constant import WorkMode


def _load_lawn_mower_module() -> ModuleType:
    """Load the mower platform without integration setup side effects."""
    package_name = "cancel_test_mammotion"
    package = ModuleType(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "mammotion")
    ]
    package.MammotionConfigEntry = object
    sys.modules[package_name] = package

    coordinator = ModuleType(f"{package_name}.coordinator")
    coordinator.MammotionReportUpdateCoordinator = object
    sys.modules[coordinator.__name__] = coordinator

    class TestMammotionBaseEntity(Entity):
        """Minimal base needed to construct the mower entity class."""

    entity = ModuleType(f"{package_name}.entity")
    entity.MammotionBaseEntity = TestMammotionBaseEntity
    sys.modules[entity.__name__] = entity

    return importlib.import_module(f"{package_name}.lawn_mower")


lawn_mower = _load_lawn_mower_module()
MammotionLawnMowerEntity = lawn_mower.MammotionLawnMowerEntity


def _ack(action: int, result: int = 0) -> LubaMsg:
    return LubaMsg(
        nav=MctlNav(
            todev_taskctrl_ack=NavTaskCtrlAck(action=action, result=result)
        )
    )


def _entity(mode: WorkMode, *responses: LubaMsg) -> MammotionLawnMowerEntity:
    entity = object.__new__(MammotionLawnMowerEntity)
    entity._command_lock = asyncio.Lock()
    entity._active_start_cancel = None
    entity._start_cancel_events = set()
    entity._start_dispatching = False
    entity._start_dispatched = False
    entity.coordinator = SimpleNamespace(
        data=SimpleNamespace(
            report_data=SimpleNamespace(
                dev=SimpleNamespace(sys_status=mode),
                work=SimpleNamespace(bp_info=1),
            )
        ),
        async_ensure_fresh_state=AsyncMock(),
        async_ensure_fresh_report_data=AsyncMock(return_value=True),
        async_send_and_wait=AsyncMock(side_effect=responses),
        async_request_report_snapshot=AsyncMock(),
    )
    return entity


@pytest.mark.asyncio
async def test_cancel_active_job_validates_pause_then_cancel() -> None:
    """An active job is paused and cancelled using acknowledged controls."""
    entity = _entity(WorkMode.MODE_WORKING)

    async def task_control(command: str, **kwargs: object) -> None:
        if command == "pause_execute_task":
            entity.coordinator.data.report_data.dev.sys_status = WorkMode.MODE_PAUSE
        elif command == "cancel_job":
            entity.coordinator.data.report_data.dev.sys_status = WorkMode.MODE_READY
            entity.coordinator.data.report_data.work.bp_info = 0

    entity._async_task_control = AsyncMock(side_effect=task_control)

    with patch.object(
        MammotionLawnMowerEntity,
        "control_state",
        new_callable=PropertyMock,
        return_value=SimpleNamespace(can_cancel=True),
    ):
        await entity.async_cancel()

    assert entity._async_task_control.await_args_list == [
        call(
            "pause_execute_task",
            action=2,
            expected_modes={WorkMode.MODE_PAUSE},
            translation_key="pause_failed",
            timeout=20,
        ),
        call(
            "cancel_job",
            action=4,
            expected_modes={WorkMode.MODE_READY},
            translation_key="command_failed",
            timeout=30,
            success_predicate=entity._async_task_control.call_args.kwargs[
                "success_predicate"
            ],
        ),
    ]
    entity.coordinator.async_request_report_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_rejects_failed_acknowledgement() -> None:
    """A nonzero device result is surfaced instead of treated as success."""
    entity = _entity(WorkMode.MODE_PAUSE)
    entity.coordinator.async_start_report_stream = AsyncMock()
    entity.coordinator.report_data_token = 1
    entity.coordinator.async_send_and_wait = AsyncMock(
        return_value=_ack(4, result=1)
    )

    with pytest.raises(HomeAssistantError):
        await entity._async_task_control(
            "cancel_job",
            action=4,
            expected_modes={WorkMode.MODE_READY},
            translation_key="command_failed",
            timeout=30,
        )

    entity.coordinator.async_send_and_wait.assert_awaited_once_with(
        "cancel_job",
        "todev_taskctrl_ack",
        preempt_reads=True,
    )
