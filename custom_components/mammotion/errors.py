"""Helpers for Mammotion device errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pymammotion.data.model.device import MowingDevice


@dataclass(frozen=True, slots=True)
class MammotionErrorDetails:
    """Describe an error reported by a Mammotion device."""

    code: int
    occurred_at: datetime | None
    message: str


def get_mammotion_error_details(
    device: MowingDevice,
    language: str,
    number: int = 1,
) -> MammotionErrorDetails | None:
    """Return a reported Mammotion error and its localised guidance."""
    errors = [
        (abs(int(code)), int(timestamp))
        for code, timestamp in zip(
            device.errors.err_code_list,
            device.errors.err_code_list_time,
            strict=False,
        )
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
        return MammotionErrorDetails(code, occurred_at, "Error message not found")

    implication = getattr(error_info, f"{language}_implication", "")
    solution = getattr(error_info, f"{language}_solution", "")
    implication = implication or error_info.en_implication
    solution = solution or error_info.en_solution
    message = f"{error_info.module}: {implication}, {solution}"
    return MammotionErrorDetails(code, occurred_at, message)
