"""Tests for persisted mower operation settings."""

import importlib.util
from pathlib import Path
from types import ModuleType

from pymammotion.data.model.device_config import OperationSettings


def _load_helpers() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "mammotion"
        / "operation_settings.py"
    )
    spec = importlib.util.spec_from_file_location("mammotion_operation_settings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


def test_setting_option_rejects_value_unsupported_by_current_firmware() -> None:
    """A restored value outside the current option set requires normalization."""
    assert helpers.option_for_value(
        ["direct_touch", "slow_touch"],
        {"direct_touch": 1, "slow_touch": 2, "less_touch": 3},
        3,
    ) is None


def test_operation_settings_round_trip() -> None:
    """Every planning field survives a storage round trip."""
    settings = OperationSettings(
        is_mow=False,
        is_dump=False,
        collect_grass_frequency=37,
        speed=0.6,
        ultra_wave=1,
        channel_mode=2,
        channel_width=31,
        rain_tactics=1,
        blade_height=45,
        toward=90,
        mowing_laps=3,
        obstacle_laps=2,
        areas=[11, 22],
    )

    payload = helpers.serialize_operation_settings(settings)

    assert helpers.deserialize_operation_settings(payload) == settings


def test_removed_area_is_pruned_from_persisted_selection() -> None:
    """An authoritative map cannot leave deleted hashes in a future route."""
    settings = OperationSettings(areas=[11, 22, 33])

    assert helpers.retain_known_areas(settings, {11, 33})
    assert settings.areas == [11, 33]
    assert not helpers.retain_known_areas(settings, {11, 33})


def test_future_settings_schema_is_ignored() -> None:
    """Unknown schema versions cannot corrupt current planning defaults."""
    assert (
        helpers.deserialize_operation_settings({"version": 99, "data": {}}) is None
    )


def test_malformed_settings_are_ignored() -> None:
    """Partial writes and invalid payloads fall back to defaults."""
    assert helpers.deserialize_operation_settings(None) is None
    assert helpers.deserialize_operation_settings({"version": 1, "data": []}) is None
