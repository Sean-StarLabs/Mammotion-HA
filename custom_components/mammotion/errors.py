"""Helpers for Mammotion device errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pymammotion.data.model.device import MowingDevice
from pymammotion.utility.constant import WorkMode
from pymammotion.utility.constant.device_constant import PosType

ERROR_ACTIVE_WINDOW = timedelta(minutes=10)


class MammotionErrorClassification(StrEnum):
    """Operational impact of a reported Mammotion error."""

    BLOCKING = "blocking"
    WARNING = "warning"
    NON_BLOCKING = "non_blocking"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class MammotionErrorDetails:
    """Describe an error reported by a Mammotion device."""

    code: int
    occurred_at: datetime | None
    message: str
    catalogue_level: int | None


def get_mammotion_error_details(
    device: MowingDevice,
    language: str,
    number: int = 1,
) -> MammotionErrorDetails | None:
    """Return a reported Mammotion error and its localised guidance."""
    timestamps = device.errors.err_code_list_time
    errors = [
        (
            abs(int(code)),
            int(timestamps[index]) if index < len(timestamps) else 0,
        )
        for index, code in enumerate(device.errors.err_code_list)
        if int(code) != 0
    ]
    if number < 1 or number > len(errors):
        return None

    code, timestamp = errors[number - 1]
    if timestamp > 100_000_000_000:
        timestamp //= 1000
    occurred_at = datetime.fromtimestamp(timestamp, UTC) if timestamp else None

    error_info = device.errors.error_codes.get(str(code))
    if error_info is None:
        return MammotionErrorDetails(code, occurred_at, "Error message not found", None)

    implication = getattr(error_info, f"{language}_implication", "")
    solution = getattr(error_info, f"{language}_solution", "")
    implication = implication or error_info.en_implication
    solution = solution or error_info.en_solution
    message = f"{error_info.module}: {implication}, {solution}"
    try:
        catalogue_level = int(error_info.level)
    except (TypeError, ValueError):
        catalogue_level = None
    return MammotionErrorDetails(code, occurred_at, message, catalogue_level)


def classify_mammotion_error(
    device: MowingDevice,
    error: MammotionErrorDetails,
    *,
    command_rejected: bool = False,
    now: datetime | None = None,
) -> MammotionErrorClassification:
    """Classify an error by its observed effect on the mower."""
    if command_rejected:
        return MammotionErrorClassification.BLOCKING

    mode = device.report_data.dev.sys_status
    if mode == WorkMode.MODE_LOCK:
        return MammotionErrorClassification.BLOCKING
    if mode == WorkMode.MODE_PAUSE and int(device.location.position_type) != int(
        PosType.CHARGE_ON
    ):
        return MammotionErrorClassification.BLOCKING

    if error.occurred_at is None:
        return MammotionErrorClassification.HISTORY
    current_time = now or datetime.now(UTC)
    if current_time - error.occurred_at > ERROR_ACTIVE_WINDOW:
        return MammotionErrorClassification.HISTORY

    if mode in {WorkMode.MODE_READY, WorkMode.MODE_INITIALIZATION}:
        return MammotionErrorClassification.BLOCKING
    if mode in {WorkMode.MODE_WORKING, WorkMode.MODE_RETURNING}:
        return MammotionErrorClassification.NON_BLOCKING
    return MammotionErrorClassification.WARNING
