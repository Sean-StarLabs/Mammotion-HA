"""Tests for Mammotion error parsing."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "mammotion_errors",
    Path(__file__).parents[1] / "custom_components/mammotion/errors.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
get_mammotion_error_details = _MODULE.get_mammotion_error_details
error_sensor_registry_migration = _MODULE.error_sensor_registry_migration


@dataclass
class _ErrorInfo:
    module: str = "Motion"
    en_implication: str = "Wheel blocked"
    en_solution: str = "Clear the wheel"
    fr_implication: str = "Roue bloquee"
    fr_solution: str = "Degager la roue"
    level: str = "2"


def _device(codes: list[int], timestamps: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        errors=SimpleNamespace(
            err_code_list=codes,
            err_code_list_time=timestamps,
            error_codes={"2801": _ErrorInfo()},
        )
    )


def test_error_number_selects_matching_nonzero_entry() -> None:
    """The requested error index is honored after empty slots are removed."""
    device = _device([0, -2801, -9999], [0, 1_725_000_000, 1_725_000_100])

    details = get_mammotion_error_details(device, "en", 2)

    assert details is not None
    assert details.code == 9999


def test_millisecond_timestamp_and_localized_guidance() -> None:
    """Cloud millisecond timestamps and available translations are normalized."""
    device = _device([-2801], [1_725_000_000_000])

    details = get_mammotion_error_details(device, "fr")

    assert details is not None
    assert details.occurred_at == datetime.fromtimestamp(1_725_000_000, UTC)
    assert details.message == "Motion, Roue bloquee, Degager la roue"
    assert details.level == "2"


def test_unknown_error_keeps_code_without_inventing_details() -> None:
    """An unknown catalogue entry remains useful and does not raise."""
    details = get_mammotion_error_details(_device([-9999], [0]), "en")

    assert details is not None
    assert details.code == 9999
    assert details.occurred_at is None
    assert details.message == "Error message not found"
    assert details.level is None


def test_error_without_matching_timestamp_keeps_code() -> None:
    """A partial timestamp report must not hide a reported error code."""
    details = get_mammotion_error_details(_device([-2801], []), "en")

    assert details is not None
    assert details.code == 2801
    assert details.occurred_at is None


def test_missing_error_returns_none() -> None:
    """An empty history produces an unavailable latest-error value."""
    assert get_mammotion_error_details(_device([0], [0]), "en") is None


def test_error_code_registry_entry_becomes_latest_error() -> None:
    """The consolidated entity preserves the legacy numeric state contract."""
    migration_source, retired = error_sensor_registry_migration(
        "yuka",
        {
            "yuka_error_1_code",
            "yuka_error_1_message",
            "yuka_error_1_time",
        },
    )

    assert migration_source == "yuka_error_1_code"
    assert retired == {"yuka_error_1_message", "yuka_error_1_time"}


def test_existing_latest_error_wins_over_legacy_entries() -> None:
    """A completed migration is idempotent and removes only stale entries."""
    migration_source, retired = error_sensor_registry_migration(
        "yuka",
        {"yuka_latest_error", "yuka_error_1_message"},
    )

    assert migration_source is None
    assert retired == {"yuka_error_1_message"}
