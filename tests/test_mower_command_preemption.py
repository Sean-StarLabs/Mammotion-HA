"""Tests for mower command preemption."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, call, patch

import pytest
from homeassistant.helpers.entity import Entity
from pymammotion.utility.constant import WorkMode


def _load_lawn_mower_module() -> ModuleType:
    """Load the mower platform without importing integration setup dependencies."""
    package_name = "custom_components.mammotion"
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


def _new_entity() -> MammotionLawnMowerEntity:
    entity = object.__new__(MammotionLawnMowerEntity)
    entity._command_lock = asyncio.Lock()
    entity._active_start_cancel = None
    entity._start_cancel_events = set()
    entity._start_dispatching = False
    entity._start_dispatched = False
    entity.coordinator = SimpleNamespace(async_request_report_snapshot=AsyncMock())
    return entity


@pytest.mark.asyncio
async def test_start_confirmation_is_preemptible() -> None:
    """A safety action does not wait for the full start report timeout."""
    entity = _new_entity()
    cancel_event = asyncio.Event()
    report_wait_cancelled = asyncio.Event()

    async def wait_for_report_data(**kwargs: object) -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            report_wait_cancelled.set()
            raise
        return False

    entity.coordinator = SimpleNamespace(
        async_wait_for_report_data=wait_for_report_data
    )
    entity._active_start_cancel = cancel_event

    task = asyncio.create_task(
        entity._async_wait_for_start_report(since=1, timeout=120)
    )
    await asyncio.sleep(0)
    cancel_event.set()

    with pytest.raises(lawn_mower._CommandPreempted):
        await asyncio.wait_for(task, timeout=1)
    assert report_wait_cancelled.is_set()


@pytest.mark.asyncio
async def test_cancelling_start_confirmation_cleans_up_report_wait() -> None:
    """Caller cancellation cannot leave a report wait running in the background."""
    entity = _new_entity()
    entity._active_start_cancel = asyncio.Event()
    report_wait_started = asyncio.Event()
    report_wait_cancelled = asyncio.Event()

    async def wait_for_report_data(**kwargs: object) -> bool:
        report_wait_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            report_wait_cancelled.set()
            raise

    entity.coordinator = SimpleNamespace(
        async_wait_for_report_data=wait_for_report_data
    )
    task = asyncio.create_task(
        entity._async_wait_for_start_report(since=1, timeout=120)
    )
    await asyncio.wait_for(report_wait_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert report_wait_cancelled.is_set()


@pytest.mark.asyncio
async def test_dispatch_is_not_cancelled_before_ack() -> None:
    """Preemption waits for an in-flight start send, then ends confirmation."""
    entity = _new_entity()
    cancel_event = asyncio.Event()
    ack = asyncio.Event()
    entity._active_start_cancel = cancel_event
    entity._start_dispatching = True

    async def dispatch_then_confirm() -> None:
        await ack.wait()
        entity._start_dispatching = False
        entity._start_dispatched = True
        if cancel_event.is_set():
            raise lawn_mower._CommandPreempted

    task = asyncio.create_task(
        entity._async_wait_unless_preempted(dispatch_then_confirm())
    )
    await asyncio.sleep(0)
    cancel_event.set()
    await asyncio.sleep(0)
    assert not task.done()

    ack.set()
    with pytest.raises(lawn_mower._CommandPreempted):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_service_cancellation_does_not_detach_start_dispatch() -> None:
    """Cancelling the caller waits for an in-flight send to finish safely."""
    entity = _new_entity()
    cancel_event = asyncio.Event()
    ack = asyncio.Event()
    dispatch_finished = asyncio.Event()
    entity._active_start_cancel = cancel_event
    entity._start_dispatching = True

    async def dispatch_then_confirm() -> None:
        try:
            await ack.wait()
            entity._start_dispatching = False
            entity._start_dispatched = True
            if cancel_event.is_set():
                raise lawn_mower._CommandPreempted
        finally:
            dispatch_finished.set()

    task = asyncio.create_task(
        entity._async_wait_unless_preempted(dispatch_then_confirm())
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not dispatch_finished.is_set()

    ack.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert dispatch_finished.is_set()


@pytest.mark.asyncio
async def test_fresh_route_allows_yuka_to_leave_dock() -> None:
    """A newly planned route uses the measured fresh-start confirmation window."""
    entity = _new_entity()
    entity.coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(areas=[1], blade_height=25),
        device_name="Yuka-Test",
        async_plan_route=AsyncMock(return_value=True),
        async_request_report_snapshot=AsyncMock(),
    )
    entity._async_task_control = AsyncMock()

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(can_cancel=False),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(sys_status=WorkMode.MODE_READY),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "report_data",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(work=SimpleNamespace(bp_info=0)),
        ),
    ):
        await entity._async_start_mowing_locked()

    entity._async_task_control.assert_awaited_once_with(
        "start_job",
        action=1,
        expected_modes={WorkMode.MODE_WORKING},
        translation_key="start_failed",
        timeout=lawn_mower.START_CONFIRM_TIMEOUT,
    )


def test_pending_start_exposes_safety_features() -> None:
    """HA routes pause and dock calls to an entity while start is pending."""
    entity = _new_entity()
    entity._start_cancel_events.add(asyncio.Event())

    with patch.object(
        MammotionLawnMowerEntity,
        "control_state",
        new_callable=PropertyMock,
        return_value=SimpleNamespace(
            can_start=False,
            can_pause=False,
            can_dock=False,
        ),
    ):
        features = entity.supported_features

    assert features & lawn_mower.LawnMowerEntityFeature.PAUSE
    assert features & lawn_mower.LawnMowerEntityFeature.DOCK


@pytest.mark.asyncio
async def test_dock_cancels_undispatched_start_without_pausing() -> None:
    """Docked planning is cancelled without sending task controls to the mower."""
    entity = _new_entity()
    cancel_event = asyncio.Event()
    entity._start_cancel_events.add(cancel_event)
    entity._async_require_fresh_state = AsyncMock()
    entity._async_task_control = AsyncMock()

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(
                can_dock=False,
                on_charger=True,
                mode=WorkMode.MODE_READY,
            ),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(sys_status=WorkMode.MODE_READY),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "report_data",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(work=SimpleNamespace(bp_info=0)),
        ),
    ):
        await entity.async_dock()

    assert cancel_event.is_set()
    entity._async_task_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_supersedes_an_accepted_unreported_start() -> None:
    """Pause is sent when start was accepted but telemetry still reports ready."""
    entity = _new_entity()
    entity._start_dispatched = True
    entity._start_cancel_events.add(asyncio.Event())
    entity._async_require_fresh_state = AsyncMock()
    entity._async_task_control = AsyncMock()

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(can_pause=False),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(sys_status=WorkMode.MODE_READY),
        ),
    ):
        await entity.async_pause()

    entity._async_task_control.assert_awaited_once_with(
        "pause_execute_task",
        action=2,
        expected_modes={WorkMode.MODE_PAUSE},
        translation_key="pause_failed",
        timeout=lawn_mower.START_PREEMPT_CONFIRM_TIMEOUT,
    )


@pytest.mark.asyncio
async def test_dock_supersedes_an_accepted_unreported_start() -> None:
    """Dock pauses an accepted start before sending the return command."""
    entity = _new_entity()
    entity._start_dispatched = True
    entity._start_cancel_events.add(asyncio.Event())
    entity._async_require_fresh_state = AsyncMock()
    entity._async_task_control = AsyncMock()

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(
                can_dock=False,
                on_charger=False,
                mode=WorkMode.MODE_READY,
            ),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(sys_status=WorkMode.MODE_READY),
        ),
    ):
        await entity.async_dock()

    assert entity._async_task_control.await_args_list == [
        call(
            "pause_execute_task",
            action=2,
            expected_modes={WorkMode.MODE_PAUSE},
            translation_key="pause_failed",
            timeout=lawn_mower.START_PREEMPT_CONFIRM_TIMEOUT,
        ),
        call(
            "return_to_dock",
            action=5,
            expected_modes={WorkMode.MODE_RETURNING},
            translation_key="dock_failed",
            timeout=30,
        ),
    ]


@pytest.mark.asyncio
async def test_dock_clears_accepted_start_paused_on_charger() -> None:
    """Dock clears a retained task after preempting Start on the charger."""
    entity = _new_entity()
    entity._start_dispatched = True
    entity._start_cancel_events.add(asyncio.Event())
    entity._async_require_fresh_state = AsyncMock()
    state = SimpleNamespace(mode=WorkMode.MODE_READY, breakpoint=0)

    async def task_control(command: str, **kwargs: object) -> None:
        if command == "pause_execute_task":
            state.mode = WorkMode.MODE_PAUSE
            state.breakpoint = 1
        elif command == "cancel_job":
            state.mode = WorkMode.MODE_READY
            state.breakpoint = 0

    entity._async_task_control = AsyncMock(side_effect=task_control)

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            side_effect=lambda: SimpleNamespace(
                can_dock=False,
                can_cancel=state.mode == WorkMode.MODE_PAUSE,
                on_charger=True,
                mode=state.mode,
            ),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            side_effect=lambda: SimpleNamespace(sys_status=state.mode),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "report_data",
            new_callable=PropertyMock,
            side_effect=lambda: SimpleNamespace(
                work=SimpleNamespace(bp_info=state.breakpoint)
            ),
        ),
    ):
        await entity.async_dock()

    assert [item.args[0] for item in entity._async_task_control.await_args_list] == [
        "pause_execute_task",
        "cancel_job",
    ]


@pytest.mark.asyncio
async def test_cancel_supersedes_an_accepted_unreported_start() -> None:
    """Cancel pauses an accepted start and then clears its retained task."""
    entity = _new_entity()
    entity._start_dispatched = True
    entity._start_cancel_events.add(asyncio.Event())
    entity._async_require_fresh_state = AsyncMock()
    state = SimpleNamespace(mode=WorkMode.MODE_READY, breakpoint=0)

    async def task_control(command: str, **kwargs: object) -> None:
        if command == "pause_execute_task":
            state.mode = WorkMode.MODE_PAUSE
            state.breakpoint = 1
        elif command == "cancel_job":
            state.mode = WorkMode.MODE_READY
            state.breakpoint = 0

    entity._async_task_control = AsyncMock(side_effect=task_control)

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(can_cancel=False),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            side_effect=lambda: SimpleNamespace(sys_status=state.mode),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "report_data",
            new_callable=PropertyMock,
            side_effect=lambda: SimpleNamespace(
                work=SimpleNamespace(bp_info=state.breakpoint)
            ),
        ),
    ):
        await entity.async_cancel()

    assert [item.args[0] for item in entity._async_task_control.await_args_list] == [
        "pause_execute_task",
        "cancel_job",
    ]
    assert entity._async_task_control.await_args_list[0].kwargs["timeout"] == (
        lawn_mower.START_PREEMPT_CONFIRM_TIMEOUT
    )


@pytest.mark.asyncio
async def test_cancel_clears_task_paused_while_charging() -> None:
    """Charging pause remains a cancellable retained-task state."""
    entity = _new_entity()
    entity._async_require_fresh_state = AsyncMock()
    entity._async_task_control = AsyncMock()

    with (
        patch.object(
            MammotionLawnMowerEntity,
            "control_state",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(can_cancel=True),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "rpt_dev_status",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(sys_status=WorkMode.MODE_CHARGING_PAUSE),
        ),
        patch.object(
            MammotionLawnMowerEntity,
            "report_data",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(work=SimpleNamespace(bp_info=1)),
        ),
    ):
        await entity.async_cancel()

    entity._async_task_control.assert_awaited_once()
    request = entity._async_task_control.await_args
    assert request.args == ("cancel_job",)
    assert request.kwargs["action"] == 4
    assert request.kwargs["expected_modes"] == {WorkMode.MODE_READY}
