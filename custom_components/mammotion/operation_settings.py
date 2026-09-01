"""Serialization helpers for mower operation settings."""

from typing import Any

from mashumaro.exceptions import InvalidFieldValue
from pymammotion.data.model.device_config import OperationSettings

SETTINGS_SCHEMA_VERSION = 1


def option_for_value(
    options: list[str], values: dict[str, int], value: int
) -> str | None:
    """Return the option whose encoded value matches a stored setting."""
    return next((option for option in options if values.get(option) == value), None)


def retain_known_areas(
    settings: OperationSettings, known_area_hashes: set[int]
) -> bool:
    """Remove selected area hashes absent from an authoritative map."""
    retained = [area for area in settings.areas if area in known_area_hashes]
    if retained == settings.areas:
        return False
    settings.areas = retained
    return True


def serialize_operation_settings(settings: OperationSettings) -> dict[str, Any]:
    """Return a versioned storage payload."""
    return {
        "version": SETTINGS_SCHEMA_VERSION,
        "data": settings.to_dict(),
    }


def deserialize_operation_settings(payload: Any) -> OperationSettings | None:
    """Restore a supported payload, ignoring malformed or future schemas."""
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != SETTINGS_SCHEMA_VERSION:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    try:
        return OperationSettings.from_dict(data)
    except (InvalidFieldValue, TypeError, ValueError):
        return None
