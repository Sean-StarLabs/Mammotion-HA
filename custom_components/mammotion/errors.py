"""Helpers for Mammotion device error history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest

from pymammotion.data.model.device import MowingDevice


@dataclass(frozen=True, slots=True)
class MammotionErrorDetails:
    """Describe one error reported by a Mammotion device."""

    code: int
    occurred_at: datetime | None
    message: str
    level: str | None


def error_sensor_registry_migration(
    unique_name: str,
    existing_unique_ids: set[str],
) -> tuple[str | None, set[str]]:
    """Plan the one-to-one error sensor migration and retired entry cleanup."""
    latest_unique_id = f"{unique_name}_latest_error"
    code_unique_id = f"{unique_name}_error_1_code"
    retired_unique_ids = {
        code_unique_id,
        f"{unique_name}_error_1_message",
        f"{unique_name}_error_1_time",
    }
    migration_source = (
        code_unique_id
        if latest_unique_id not in existing_unique_ids
        and code_unique_id in existing_unique_ids
        else None
    )
    if migration_source is not None:
        retired_unique_ids.remove(migration_source)
    return migration_source, retired_unique_ids & existing_unique_ids


def get_mammotion_error_details(
    device: MowingDevice,
    language: str,
    number: int = 1,
) -> MammotionErrorDetails | None:
    """Return a reported Mammotion error and its localized guidance."""
    errors = [
        (abs(int(code)), int(timestamp))
        for code, timestamp in zip_longest(
            device.errors.err_code_list,
            device.errors.err_code_list_time,
            fillvalue=0,
        )
        if int(code) != 0
    ]
    if number < 1 or number > len(errors):
        return None

    code, timestamp = errors[number - 1]
    if timestamp > 100_000_000_000:
        timestamp //= 1000
    try:
        occurred_at = datetime.fromtimestamp(timestamp, UTC) if timestamp else None
    except (OSError, OverflowError, ValueError):
        occurred_at = None

    error_info = device.errors.error_codes.get(str(code))
    if error_info is None:
        return MammotionErrorDetails(code, occurred_at, "Error message not found", None)

    implication = getattr(error_info, f"{language}_implication", "") or error_info.en_implication
    solution = getattr(error_info, f"{language}_solution", "") or error_info.en_solution
    message = ", ".join(part for part in (error_info.module, implication, solution) if part)
    level = str(error_info.level) if error_info.level not in (None, "") else None
    return MammotionErrorDetails(code, occurred_at, message, level)
