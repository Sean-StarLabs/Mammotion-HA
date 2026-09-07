"""Tests for transport recovery switches."""

from .switch_test_support import load_switch_module


def test_connectivity_switch_remains_available_without_telemetry() -> None:
    """Turning off the final transport cannot hide its recovery control."""
    module = load_switch_module()
    entity = object.__new__(module.MammotionConnectivitySwitchEntity)

    assert entity.available
