"""Tests for primary mower task attributes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "mammotion_mower_attributes",
    Path(__file__).parents[1] / "custom_components/mammotion/mower_attributes.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
mower_task_attributes = _MODULE.mower_task_attributes


def test_task_metrics_and_area_statuses_are_primary_attributes() -> None:
    """Packed task values and per-area state are exposed without extra sensors."""
    mower = SimpleNamespace(
        report_data=SimpleNamespace(
            work=SimpleNamespace(
                area=42 << 16,
                progress=(18 << 16) | 30,
            )
        ),
        location=SimpleNamespace(work_zone=101),
        events=SimpleNamespace(
            work_tasks_event=SimpleNamespace(
                hash_area_map={101: SimpleNamespace(name="working")}
            )
        ),
    )

    area_names = {101: "Back garden"}
    attributes = mower_task_attributes(mower, area_names.get)

    assert attributes == {
        "cleaning_progress": 42,
        "current_area": "Back garden",
        "total_time_minutes": 30,
        "elapsed_time_minutes": 12,
        "remaining_time_minutes": 18,
        "task_areas": {
            "101": {
                "name": "Back garden",
                "status": "working",
            }
        },
    }


def test_task_area_keys_survive_duplicate_names_and_renames() -> None:
    """Area hashes keep every status stable when display names are not unique."""
    mower = SimpleNamespace(
        report_data=SimpleNamespace(
            work=SimpleNamespace(area=0, progress=0)
        ),
        location=SimpleNamespace(work_zone=101),
        events=SimpleNamespace(
            work_tasks_event=SimpleNamespace(
                hash_area_map={
                    101: SimpleNamespace(name="working"),
                    202: SimpleNamespace(name="pending"),
                }
            )
        ),
    )

    duplicate_names = mower_task_attributes(mower, lambda _area_hash: "Garden")
    renamed = mower_task_attributes(
        mower,
        lambda area_hash: {101: "Rear lawn", 202: "Side lawn"}[area_hash],
    )

    assert duplicate_names["task_areas"] == {
        "101": {"name": "Garden", "status": "working"},
        "202": {"name": "Garden", "status": "pending"},
    }
    assert renamed["task_areas"] == {
        "101": {"name": "Rear lawn", "status": "working"},
        "202": {"name": "Side lawn", "status": "pending"},
    }


def test_elapsed_time_never_becomes_negative() -> None:
    """Incomplete telemetry cannot expose a negative duration."""
    mower = SimpleNamespace(
        report_data=SimpleNamespace(
            work=SimpleNamespace(area=0, progress=(20 << 16) | 5)
        ),
        location=SimpleNamespace(work_zone=0),
        events=SimpleNamespace(
            work_tasks_event=SimpleNamespace(hash_area_map={})
        ),
    )

    assert mower_task_attributes(mower, lambda _area_hash: None)[
        "elapsed_time_minutes"
    ] == 0
