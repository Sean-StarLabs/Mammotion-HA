"""Serialization helpers for mower operation settings."""

from dataclasses import replace
from typing import Any

from mashumaro.exceptions import InvalidFieldValue
from pymammotion.data.model.device_config import OperationSettings

SETTINGS_SCHEMA_VERSION = 1


def option_for_value(
    options: list[str], values: dict[str, int], value: int
) -> str | None:
    """Return the option whose encoded value matches a stored setting."""
    return next((option for option in options if values.get(option) == value), None)


def clone_operation_settings(settings: OperationSettings) -> OperationSettings:
    """Return an independent settings snapshot for route generation."""
    return replace(settings, areas=list(settings.areas))


def retain_known_areas(
    settings: OperationSettings, known_area_hashes: set[int]
) -> bool:
    """Remove selected area hashes absent from an authoritative map."""
    retained = [area for area in settings.areas if area in known_area_hashes]
    if retained == settings.areas:
        return False
    settings.areas = retained
    return True


def should_restore_number_state(
    *, route_setting: bool, operation_settings_restored: bool
) -> bool:
    """Return whether a number should use its legacy entity-state fallback."""
    return not route_setting or not operation_settings_restored


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
