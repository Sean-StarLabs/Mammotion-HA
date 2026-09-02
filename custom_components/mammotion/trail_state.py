"""Native mower trail persistence helpers."""

from __future__ import annotations

from typing import Any

_FINGERPRINT_MASK = (1 << 64) - 1


def _mow_path_revision(mow_path: Any) -> tuple[object, ...]:
    """Return compact transfer metadata that changes with complete route frames."""
    return tuple(
        (
            transaction_id,
            tuple(
                (
                    frame_id,
                    getattr(frame, "transaction_id", 0),
                    getattr(frame, "current_frame", 0),
                    getattr(frame, "total_frame", 0),
                    getattr(frame, "data_hash", 0),
                    getattr(frame, "data_len", 0),
                    tuple(
                        (
                            packet.path_hash,
                            packet.path_cur,
                            packet.path_total,
                            len(packet.data_couple),
                        )
                        for packet in getattr(frame, "path_packets", ())
                    ),
                )
                for frame_id, frame in sorted(frames.items())
            ),
        )
        for transaction_id, frames in sorted(mow_path.items())
    )


def _dynamics_line_revision(points: Any) -> tuple[int, int]:
    """Fingerprint live points without copying the complete coordinate list."""
    fingerprint = 0
    for point in points:
        if isinstance(point, dict):
            coordinates = (point.get("x"), point.get("y"))
        else:
            coordinates = (getattr(point, "x", None), getattr(point, "y", None))
        fingerprint = (
            (fingerprint * 1_000_003) ^ hash(coordinates)
        ) & _FINGERPRINT_MASK
    return len(points), fingerprint


def native_trail_signature(
    map_data: Any,
) -> tuple[object, ...]:
    """Return lightweight native-geometry revisions and task identities.

    Cover-path frames carry hashes and immutable transfer metadata. Live trail
    points need a small rolling fingerprint because released clients may mutate
    that list in place. Generated GeoJSON objects are replaced wholesale.
    """
    return (
        _mow_path_revision(map_data.current_mow_path),
        _dynamics_line_revision(map_data.dynamics_line),
        id(map_data.generated_mow_path_geojson),
        id(map_data.generated_mow_progress_geojson),
        id(map_data.generated_dynamics_line_geojson),
        int(getattr(map_data, "current_mow_path_session_id", 0)),
        int(getattr(map_data, "dynamics_line_session_id", 0)),
    )
