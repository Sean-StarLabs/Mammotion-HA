"""Mower task attributes exposed on the primary entity."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def mower_task_attributes(
    mower: Any,
    area_name: Callable[[int], str | None],
) -> dict[str, object]:
    """Return current task details without creating duplicate sensor entities."""
    work = mower.report_data.work
    total_minutes = int(work.progress) & 0xFFFF
    remaining_minutes = int(work.progress) >> 16
    current_area = area_name(int(mower.location.work_zone))
    task_areas = {
        str(area_hash): {
            "name": area_name(int(area_hash)) or str(area_hash),
            "status": getattr(status, "name", None),
        }
        for area_hash, status in mower.events.work_tasks_event.hash_area_map.items()
    }
    return {
        "cleaning_progress": int(work.area) >> 16,
        "current_area": current_area,
        "total_time_minutes": total_minutes,
        "elapsed_time_minutes": max(total_minutes - remaining_minutes, 0),
        "remaining_time_minutes": remaining_minutes,
        "task_areas": task_areas,
    }
