"""Tests for native mower trail revision tracking."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "mammotion_trail_state",
    Path(__file__).parents[1] / "custom_components/mammotion/trail_state.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
native_trail_signature = _MODULE.native_trail_signature


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


def test_unrelated_telemetry_does_not_change_trail_revision() -> None:
    """A stable map object does not schedule writes on every telemetry tick."""
    map_data = _map()

    assert native_trail_signature(map_data) == native_trail_signature(map_data)


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
