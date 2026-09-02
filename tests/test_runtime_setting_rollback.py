"""Tests for rejected runtime mower-setting updates."""

from __future__ import annotations

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

    with pytest.raises(RuntimeError, match="rejected"):
        await entity.async_set_native_value(0.4)

    assert entity._attr_native_value == 0.2  # noqa: SLF001
    assert coordinator.operation_settings.speed == 0.2
    coordinator.async_save_operation_settings.assert_not_called()


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

    with pytest.raises(RuntimeError, match="rejected"):
        await entity.async_select_option("custom")

    assert entity._attr_current_option == "random"  # noqa: SLF001
    assert coordinator.operation_settings.pattern == "random"
    coordinator.async_save_operation_settings.assert_not_called()
