"""Tests for rejected runtime mower-setting updates."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_entity_modules() -> tuple[ModuleType, ModuleType]:
    """Load entity platforms without importing integration setup dependencies."""
    package_name = "runtime_setting_test_mammotion"
    package = ModuleType(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "mammotion")
    ]
    package.MammotionConfigEntry = object
    package.MammotionReportUpdateCoordinator = object
    sys.modules[package_name] = package

    coordinator = ModuleType(f"{package_name}.coordinator")
    coordinator.MammotionBaseUpdateCoordinator = object
    coordinator.MammotionSpinoCoordinator = object
    sys.modules[coordinator.__name__] = coordinator

    entity = ModuleType(f"{package_name}.entity")
    entity.MammotionBaseEntity = type("MammotionBaseEntity", (), {})
    entity.MammotionBaseSpinoEntity = type("MammotionBaseSpinoEntity", (), {})
    sys.modules[entity.__name__] = entity

    return (
        importlib.import_module(f"{package_name}.number"),
        importlib.import_module(f"{package_name}.select"),
    )


NUMBER_MODULE, SELECT_MODULE = _load_entity_modules()


@pytest.mark.asyncio
async def test_rejected_number_update_restores_entity_and_settings() -> None:
    """A rejected numeric update is neither displayed nor persisted."""
    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(speed=0.2),
        async_save_operation_settings=MagicMock(),
    )

    def set_value(_coordinator: object, value: float) -> None:
        coordinator.operation_settings.speed = value

    entity = object.__new__(NUMBER_MODULE.MammotionWorkingNumberEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=set_value,
        set_async_fn=AsyncMock(side_effect=RuntimeError("rejected")),
    )
    entity._attr_native_value = 0.2  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(RuntimeError, match="rejected"):
        await entity.async_set_native_value(0.4)

    assert entity._attr_native_value == 0.2  # noqa: SLF001
    assert coordinator.operation_settings.speed == 0.2
    entity.async_write_ha_state.assert_called_once_with()
    coordinator.async_save_operation_settings.assert_not_called()


@pytest.mark.asyncio
async def test_overlapping_number_updates_cannot_rollback_newer_value() -> None:
    """A rejected request completes before a later update can be applied."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def send_value(_coordinator: object, value: float) -> None:
        if value == 0.4:
            first_started.set()
            await release_first.wait()
            raise RuntimeError("rejected")

    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(speed=0.2),
        async_save_operation_settings=MagicMock(),
    )

    def set_value(_coordinator: object, value: float) -> None:
        coordinator.operation_settings.speed = value

    entity = object.__new__(NUMBER_MODULE.MammotionWorkingNumberEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=set_value,
        set_async_fn=send_value,
    )
    entity._attr_native_value = 0.2  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    first = asyncio.create_task(entity.async_set_native_value(0.4))
    await first_started.wait()
    second = asyncio.create_task(entity.async_set_native_value(0.6))
    release_first.set()

    with pytest.raises(RuntimeError, match="rejected"):
        await first
    await second

    assert entity._attr_native_value == 0.6  # noqa: SLF001
    assert coordinator.operation_settings.speed == 0.6
    coordinator.async_save_operation_settings.assert_called_once_with()


@pytest.mark.asyncio
async def test_rejected_select_update_restores_entity_and_settings() -> None:
    """A rejected select update is neither displayed nor persisted."""
    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(pattern="random"),
        async_save_operation_settings=MagicMock(),
    )

    def set_option(_coordinator: object, option: str) -> None:
        coordinator.operation_settings.pattern = option

    entity = object.__new__(SELECT_MODULE.MammotionConfigSelectEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=set_option,
        async_set_fn=AsyncMock(side_effect=RuntimeError("rejected")),
    )
    entity._attr_current_option = "random"  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(RuntimeError, match="rejected"):
        await entity.async_select_option("custom")

    assert entity._attr_current_option == "random"  # noqa: SLF001
    assert coordinator.operation_settings.pattern == "random"
    entity.async_write_ha_state.assert_called_once_with()
    coordinator.async_save_operation_settings.assert_not_called()


@pytest.mark.asyncio
async def test_matching_number_value_is_sent_during_active_job() -> None:
    """A saved default can still replace a different per-route runtime value."""
    send_value = AsyncMock()
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            report_data=SimpleNamespace(
                dev=SimpleNamespace(sys_status=NUMBER_MODULE.WorkMode.MODE_WORKING)
            )
        ),
        operation_settings=SimpleNamespace(speed=0.2),
        async_save_operation_settings=MagicMock(),
    )
    entity = object.__new__(NUMBER_MODULE.MammotionWorkingNumberEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(set_fn=None, set_async_fn=send_value)
    entity._attr_native_value = 0.2  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(0.2)

    send_value.assert_awaited_once_with(coordinator, 0.2)


@pytest.mark.asyncio
async def test_matching_select_value_is_sent_during_active_job() -> None:
    """Selecting the saved default can update an overridden active route."""
    send_option = AsyncMock()
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            report_data=SimpleNamespace(
                dev=SimpleNamespace(sys_status=SELECT_MODULE.WorkMode.MODE_WORKING)
            )
        ),
        operation_settings=SimpleNamespace(pattern="random"),
        async_save_operation_settings=MagicMock(),
    )
    entity = object.__new__(SELECT_MODULE.MammotionConfigSelectEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=lambda *_args: None,
        async_set_fn=send_option,
    )
    entity._attr_current_option = "random"  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    await entity.async_select_option("random")

    send_option.assert_awaited_once_with(coordinator)


@pytest.mark.asyncio
async def test_overlapping_select_updates_cannot_rollback_newer_option() -> None:
    """A rejected request completes before a later option can be applied."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def send_option(_coordinator: object) -> None:
        if coordinator.operation_settings.pattern == "custom":
            first_started.set()
            await release_first.wait()
            raise RuntimeError("rejected")

    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(pattern="random"),
        async_save_operation_settings=MagicMock(),
    )

    def set_option(_coordinator: object, option: str) -> None:
        coordinator.operation_settings.pattern = option

    entity = object.__new__(SELECT_MODULE.MammotionConfigSelectEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=set_option,
        async_set_fn=send_option,
    )
    entity._attr_current_option = "random"  # noqa: SLF001
    entity._setting_lock = asyncio.Lock()  # noqa: SLF001
    entity.async_write_ha_state = MagicMock()

    first = asyncio.create_task(entity.async_select_option("custom"))
    await first_started.wait()
    second = asyncio.create_task(entity.async_select_option("parallel"))
    release_first.set()

    with pytest.raises(RuntimeError, match="rejected"):
        await first
    await second

    assert entity._attr_current_option == "parallel"  # noqa: SLF001
    assert coordinator.operation_settings.pattern == "parallel"
    coordinator.async_save_operation_settings.assert_called_once_with()


@pytest.mark.asyncio
async def test_different_runtime_controls_share_one_settings_lock() -> None:
    """A number update and select update cannot send mixed settings snapshots."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    sent: list[tuple[float, str]] = []
    shared_lock = asyncio.Lock()
    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(speed=0.2, pattern="random"),
        async_save_operation_settings=MagicMock(),
    )

    async def send_number(_coordinator: object, _value: float) -> None:
        sent.append((coordinator.operation_settings.speed, coordinator.operation_settings.pattern))
        first_started.set()
        await release_first.wait()

    async def send_select(_coordinator: object) -> None:
        sent.append((coordinator.operation_settings.speed, coordinator.operation_settings.pattern))

    number = object.__new__(NUMBER_MODULE.MammotionWorkingNumberEntity)
    number.coordinator = coordinator
    number.entity_description = SimpleNamespace(
        set_fn=lambda _coordinator, value: setattr(
            coordinator.operation_settings, "speed", value
        ),
        set_async_fn=send_number,
    )
    number._attr_native_value = 0.2  # noqa: SLF001
    number._setting_lock = shared_lock  # noqa: SLF001
    number.async_write_ha_state = MagicMock()

    select = object.__new__(SELECT_MODULE.MammotionConfigSelectEntity)
    select.coordinator = coordinator
    select.entity_description = SimpleNamespace(
        set_fn=lambda _coordinator, option: setattr(
            coordinator.operation_settings, "pattern", option
        ),
        async_set_fn=send_select,
    )
    select._attr_current_option = "random"  # noqa: SLF001
    select._setting_lock = shared_lock  # noqa: SLF001
    select.async_write_ha_state = MagicMock()

    number_task = asyncio.create_task(number.async_set_native_value(0.4))
    await first_started.wait()
    select_task = asyncio.create_task(select.async_select_option("parallel"))
    await asyncio.sleep(0)
    assert sent == [(0.4, "random")]

    release_first.set()
    await number_task
    await select_task

    assert sent == [(0.4, "random"), (0.4, "parallel")]
