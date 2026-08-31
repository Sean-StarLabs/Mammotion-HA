"""Tests for acknowledged mower cancellation."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, call

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
        nav=MctlNav(todev_taskctrl_ack=NavTaskCtrlAck(action=action, result=result))
    )


def _entity(mode: WorkMode, *responses: LubaMsg | None) -> MammotionLawnMowerEntity:
    entity = object.__new__(MammotionLawnMowerEntity)
    entity.coordinator = SimpleNamespace(
        data=SimpleNamespace(
            report_data=SimpleNamespace(
                dev=SimpleNamespace(sys_status=mode),
            )
        ),
        async_ensure_fresh_state=AsyncMock(),
        async_send_and_wait=AsyncMock(side_effect=responses),
        async_request_report_snapshot=AsyncMock(),
    )
    return entity


@pytest.mark.asyncio
async def test_cancel_active_job_validates_pause_then_cancel() -> None:
    """An active job is paused and cancelled using acknowledged controls."""
    entity = _entity(WorkMode.MODE_WORKING, _ack(2), _ack(4))

    await entity.async_cancel()

    assert entity.coordinator.async_send_and_wait.await_args_list == [
        call("pause_execute_task", "todev_taskctrl_ack"),
        call("cancel_job", "todev_taskctrl_ack"),
    ]
    entity.coordinator.async_request_report_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_continues_when_pause_ack_times_out() -> None:
    """An applied pause with a lost ACK must not prevent cancellation."""
    entity = _entity(WorkMode.MODE_WORKING, None, _ack(4))

    await entity.async_cancel()

    assert entity.coordinator.async_send_and_wait.await_args_list == [
        call("pause_execute_task", "todev_taskctrl_ack"),
        call("cancel_job", "todev_taskctrl_ack"),
    ]
    entity.coordinator.async_request_report_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_rejects_missing_final_acknowledgement() -> None:
    """A transport failure cannot make an unconfirmed cancellation succeed."""
    entity = _entity(WorkMode.MODE_WORKING, None, None)

    with pytest.raises(HomeAssistantError):
        await entity.async_cancel()

    assert entity.coordinator.async_send_and_wait.await_args_list == [
        call("pause_execute_task", "todev_taskctrl_ack"),
        call("cancel_job", "todev_taskctrl_ack"),
    ]
    entity.coordinator.async_request_report_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_rejects_failed_acknowledgement() -> None:
    """A nonzero device result is surfaced instead of treated as success."""
    entity = _entity(WorkMode.MODE_PAUSE, _ack(4, result=1))

    with pytest.raises(HomeAssistantError):
        await entity.async_cancel()

    entity.coordinator.async_request_report_snapshot.assert_awaited_once()
