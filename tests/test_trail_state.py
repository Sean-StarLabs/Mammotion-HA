"""Tests for native mower trail revision tracking."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "mammotion_trail_state",
    Path(__file__).parents[1] / "custom_components/mammotion/trail_state.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
native_trail_signature = _MODULE.native_trail_signature


def _load_coordinator_module() -> ModuleType:
    """Load the coordinator without integration setup side effects."""
    package_name = "trail_test_mammotion"
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
MammotionReportUpdateCoordinator = coordinator_module.MammotionReportUpdateCoordinator


def _map() -> SimpleNamespace:
    return SimpleNamespace(
        current_mow_path={},
        dynamics_line=[],
        generated_mow_path_geojson={},
        generated_mow_progress_geojson={},
        generated_dynamics_line_geojson={},
        current_mow_path_session_id=0,
        dynamics_line_session_id=0,
    )


@pytest.mark.asyncio
async def test_only_native_trail_changes_schedule_persistence() -> None:
    """Initial, replaced, and cleared geometry save without telemetry churn."""
    map_data = _map()
    snapshot = SimpleNamespace(raw=SimpleNamespace(map=map_data))
    coordinator = object.__new__(MammotionReportUpdateCoordinator)
    coordinator._last_native_trail_signature = None
    coordinator.async_save_data = MagicMock()

    with patch.object(
        MammotionBaseUpdateCoordinator,
        "_on_state_changed",
        new=AsyncMock(),
    ):
        await coordinator._on_state_changed(snapshot)
        coordinator.async_save_data.assert_called_once_with(snapshot.raw)

        await coordinator._on_state_changed(snapshot)
        assert coordinator.async_save_data.call_count == 1

        map_data.generated_dynamics_line_geojson = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature"}],
        }
        await coordinator._on_state_changed(snapshot)
        assert coordinator.async_save_data.call_count == 2

        map_data.generated_dynamics_line_geojson = {}
        await coordinator._on_state_changed(snapshot)
        assert coordinator.async_save_data.call_count == 3


def test_replaced_native_geometry_changes_trail_revision() -> None:
    """Publishing or clearing a native route schedules durable state storage."""
    map_data = _map()
    before = native_trail_signature(map_data)

    map_data.generated_dynamics_line_geojson = {
        "type": "FeatureCollection",
        "features": [],
    }

    assert native_trail_signature(map_data) != before


def test_raw_native_geometry_changes_before_geojson_is_renderable() -> None:
    """Raw updates are persisted even while RTK-based GeoJSON stays empty."""
    map_data = _map()
    before = native_trail_signature(map_data)

    map_data.current_mow_path = {7: {1: object()}}
    map_data.dynamics_line = [object()]

    assert map_data.generated_mow_path_geojson == {}
    assert map_data.generated_dynamics_line_geojson == {}
    assert native_trail_signature(map_data) != before


def test_new_mow_session_changes_trail_revision() -> None:
    """A new mowing lifecycle is persisted even before geometry arrives."""
    map_data = _map()
    before = native_trail_signature(map_data)

    map_data.current_mow_path_session_id = 4

    assert native_trail_signature(map_data) != before


def test_older_map_models_default_missing_session_ids() -> None:
    """Persistence remains usable while the pymammotion dependency is updated."""
    map_data = _map()
    del map_data.current_mow_path_session_id
    del map_data.dynamics_line_session_id

    assert native_trail_signature(map_data)[-2:] == (0, 0)
