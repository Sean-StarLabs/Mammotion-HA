"""Native mower trail persistence helpers."""

from __future__ import annotations

from typing import Any


def native_trail_signature(
    map_data: Any,
) -> tuple[object, object, object, object, object, int, int]:
    """Return serialized native geometry and its reported task identities.

    PyMammotion atomically replaces the published raw containers. Retaining them
    in the previous signature makes equality content-aware without walking every
    point on each unrelated telemetry update.
    """
    return (
        map_data.current_mow_path,
        map_data.dynamics_line,
        map_data.generated_mow_path_geojson,
        map_data.generated_mow_progress_geojson,
        map_data.generated_dynamics_line_geojson,
        int(getattr(map_data, "current_mow_path_session_id", 0)),
        int(getattr(map_data, "dynamics_line_session_id", 0)),
    )
