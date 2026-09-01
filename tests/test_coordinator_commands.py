"""Tests for coordinator command response handling."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.entity import Entity
from pymammotion.state.device_state import DeviceConnectionState


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


coordinator_module = _load_coordinator_module()
MammotionBaseUpdateCoordinator = coordinator_module.MammotionBaseUpdateCoordinator


def _load_entity_module() -> ModuleType:
    """Load the base entity without optional camera image dependencies."""
    camera_module = ModuleType("homeassistant.components.camera")

    class TestCamera(Entity):
        """Minimal camera base required by the entity module."""

    camera_module.Camera = TestCamera
    camera_module.CameraEntityFeature = type(
        "CameraEntityFeature", (), {"ON_OFF": 1, "STREAM": 2}
    )
    previous_camera = sys.modules.get(camera_module.__name__)
    sys.modules[camera_module.__name__] = camera_module
    try:
        return importlib.import_module("coordinator_test_mammotion.entity")
    finally:
        if previous_camera is None:
            del sys.modules[camera_module.__name__]
        else:
            sys.modules[camera_module.__name__] = previous_camera


MammotionBaseEntity = _load_entity_module().MammotionBaseEntity


class _TestCoordinator(MammotionBaseUpdateCoordinator):
    def get_coordinator_data(self, device: object) -> object:
        return device


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


@pytest.mark.parametrize(
    ("connection_state", "has_usable_transport", "expected_available"),
    [
        (DeviceConnectionState.CONNECTED, True, True),
        (DeviceConnectionState.CONNECTED, False, True),
        (DeviceConnectionState.DISCONNECTED, True, False),
        (DeviceConnectionState.DISCONNECTED, False, False),
    ],
)
def test_reported_availability_is_independent_from_command_readiness(
    connection_state: DeviceConnectionState,
    has_usable_transport: bool,
    expected_available: bool,
) -> None:
    """Reported connectivity and usable command transport remain independent."""
    handle = MagicMock()
    handle.snapshot.connection_state = connection_state
    handle.has_usable_transport = has_usable_transport

    coordinator = object.__new__(_TestCoordinator)
    coordinator.manager = MagicMock()
    coordinator.manager.get_device_by_name.return_value = object()
    coordinator.manager.mower.return_value = handle
    coordinator.device_name = "Yuka-Test"
    coordinator.data = object()

    entity = object.__new__(MammotionBaseEntity)
    entity.coordinator = coordinator

    assert coordinator.is_online() is expected_available
    assert coordinator.command_ready is has_usable_transport
    assert entity.available is expected_available
