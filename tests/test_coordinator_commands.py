"""Tests for coordinator command response handling."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_coordinator_module() -> ModuleType:
    """Load the coordinator without integration setup side effects."""
    package_name = "coordinator_test_mammotion"
    package = ModuleType(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "mammotion")
    ]
    sys.modules[package_name] = package
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.BluetoothCallbackMatcher = object
    bluetooth_module.BluetoothChange = object
    bluetooth_module.async_register_callback = MagicMock()
    previous_bluetooth = sys.modules.get(bluetooth_module.__name__)
    sys.modules[bluetooth_module.__name__] = bluetooth_module
    try:
        return importlib.import_module(f"{package_name}.coordinator")
    finally:
        if previous_bluetooth is None:
            del sys.modules[bluetooth_module.__name__]
        else:
            sys.modules[bluetooth_module.__name__] = previous_bluetooth


MammotionBaseUpdateCoordinator = (
    _load_coordinator_module().MammotionBaseUpdateCoordinator
)


class _TestCoordinator(MammotionBaseUpdateCoordinator):
    def get_coordinator_data(self, device: object) -> object:
        return device


def test_area_selection_refreshes_dependent_entities() -> None:
    """Area changes immediately refresh the mower's supported controls."""
    coordinator = object.__new__(_TestCoordinator)
    coordinator.async_save_operation_settings = MagicMock()
    coordinator.async_update_listeners = MagicMock()

    coordinator.async_area_selection_changed()

    coordinator.async_save_operation_settings.assert_called_once_with()
    coordinator.async_update_listeners.assert_called_once_with()


@pytest.mark.asyncio
async def test_send_and_wait_returns_protocol_response() -> None:
    """Callers receive the response needed to validate an acknowledgement."""
    response = object()
    coordinator = object.__new__(_TestCoordinator)
    coordinator.manager = MagicMock()
    coordinator.manager.get_device_by_name.return_value = object()
    coordinator.manager.send_command_and_wait = AsyncMock(return_value=response)
    coordinator.device_name = "Yuka-Test"
    coordinator._bluetooth_enabled = True  # noqa: SLF001
    coordinator.is_online = MagicMock(return_value=True)

    result = await coordinator.async_send_and_wait("cancel_job", "task_ack")

    assert result is response
