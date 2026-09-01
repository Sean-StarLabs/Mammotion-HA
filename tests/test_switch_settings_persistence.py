"""Tests for persisted route-setting switches."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from .switch_test_support import load_switch_module


@pytest.mark.asyncio
async def test_route_switch_updates_persisted_operation_settings() -> None:
    """A route toggle survives entity and Home Assistant reloads."""
    module = load_switch_module()
    coordinator = SimpleNamespace(
        operation_settings=SimpleNamespace(is_mow=False),
        async_save_operation_settings=MagicMock(),
    )
    entity = object.__new__(module.MammotionConfigSwitchEntity)
    entity.coordinator = coordinator
    entity.entity_description = SimpleNamespace(
        set_fn=lambda target, value: setattr(target.operation_settings, "is_mow", value)
    )
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coordinator.operation_settings.is_mow
    coordinator.async_save_operation_settings.assert_called_once_with()
