"""Tests for cloud authentication fallback with retained BLE support."""

# Setup helpers are intentionally exercised at their private integration boundary.
# ruff: noqa: SLF001

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import homeassistant.components as ha_components
import pytest
from aiohttp import ClientConnectorError
from homeassistant.exceptions import ConfigEntryNotReady
from pymammotion.transport.base import LoginFailedError


def _load_integration_module() -> ModuleType:
    """Load integration setup without importing HA's optional USB platform."""
    package_name = "cloud_ble_test_mammotion"
    package_path = Path(__file__).parents[1] / "custom_components" / "mammotion"
    bluetooth_name = "homeassistant.components.bluetooth"
    bluetooth_module = ModuleType(bluetooth_name)
    bluetooth_module.BluetoothCallbackMatcher = object
    bluetooth_module.BluetoothChange = object
    bluetooth_module.BluetoothScanningMode = object
    bluetooth_module.BluetoothServiceInfoBleak = object
    bluetooth_module.async_register_callback = MagicMock()

    previous_module = sys.modules.get(bluetooth_name)
    previous_attribute = getattr(ha_components, "bluetooth", None)
    sys.modules[bluetooth_name] = bluetooth_module
    ha_components.bluetooth = bluetooth_module
    try:
        spec = importlib.util.spec_from_file_location(
            package_name,
            package_path / "__init__.py",
            submodule_search_locations=[str(package_path)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_module is None:
            del sys.modules[bluetooth_name]
        else:
            sys.modules[bluetooth_name] = previous_module
        if previous_attribute is None:
            delattr(ha_components, "bluetooth")
        else:
            ha_components.bluetooth = previous_attribute


integration = _load_integration_module()


def _entry(data: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        data=data or {},
        entry_id="entry-1",
        async_on_unload=MagicMock(),
        async_start_reauth=MagicMock(),
    )


def _hass() -> SimpleNamespace:
    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=MagicMock(),
            async_reload=AsyncMock(),
        )
    )


@pytest.mark.asyncio
async def test_cached_login_still_discovers_new_devices() -> None:
    """Credential restore does not freeze the cloud device inventory."""
    entry = _entry({integration.CONF_AEP_DATA: {"cached": True}})
    mammotion = SimpleNamespace(
        restore_credentials=AsyncMock(),
        login_and_initiate_cloud=AsyncMock(),
    )

    with patch.object(
        integration.aiohttp_client,
        "async_get_clientsession",
        return_value=object(),
    ):
        assert await integration._async_attempt_login(
            _hass(),
            entry,
            mammotion,
            "account",
            "password",
            ble_fallback=True,
        )

    assert mammotion.restore_credentials.await_args.kwargs[
        "check_for_new_devices"
    ] is True
    mammotion.login_and_initiate_cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_connectivity_failure_keeps_ble_and_schedules_short_retry() -> None:
    """An unreachable cloud does not defer BLE-backed setup."""
    error = ClientConnectorError(
        MagicMock(host="example.test", port=443, ssl=True),
        OSError("offline"),
    )
    mammotion = SimpleNamespace(
        login_and_initiate_cloud=AsyncMock(side_effect=error)
    )
    hass = _hass()
    entry = _entry()
    schedule_retry = MagicMock()

    with (
        patch.object(
            integration.aiohttp_client,
            "async_get_clientsession",
            return_value=object(),
        ),
        patch.object(integration, "_schedule_cloud_retry", schedule_retry),
    ):
        result = await integration._async_attempt_login(
            hass,
            entry,
            mammotion,
            "account",
            "password",
            ble_fallback=True,
        )

    assert result is False
    schedule_retry.assert_called_once_with(
        hass,
        entry,
        timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_connectivity_failure_without_ble_defers_setup() -> None:
    """Cloud-only entries retain Home Assistant's normal retry behavior."""
    error = ClientConnectorError(
        MagicMock(host="example.test", port=443, ssl=True),
        OSError("offline"),
    )
    mammotion = SimpleNamespace(
        login_and_initiate_cloud=AsyncMock(side_effect=error)
    )

    with (
        patch.object(
            integration.aiohttp_client,
            "async_get_clientsession",
            return_value=object(),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await integration._async_attempt_login(
            _hass(),
            _entry(),
            mammotion,
            "account",
            "password",
            ble_fallback=False,
        )


@pytest.mark.asyncio
async def test_scheduled_cloud_retry_reloads_entry() -> None:
    """The fallback timer is owned by the entry and reloads that entry once."""
    hass = _hass()
    entry = _entry()
    unsubscribe = MagicMock()
    captured_callback: object | None = None

    def schedule(
        _hass_arg: object,
        delay: float,
        callback: object,
    ) -> object:
        nonlocal captured_callback
        assert delay == 300
        captured_callback = callback
        return unsubscribe

    with patch.object(integration, "async_call_later", side_effect=schedule):
        integration._schedule_cloud_retry(hass, entry, timedelta(minutes=5))

    entry.async_on_unload.assert_called_once_with(unsubscribe)
    assert callable(captured_callback)
    await captured_callback(datetime.now(UTC))
    hass.config_entries.async_reload.assert_awaited_once_with("entry-1")


@pytest.mark.asyncio
async def test_auth_failure_backs_off_without_disabling_cloud_account() -> None:
    """BLE fallback retains cloud intent and starts reauthentication."""
    entry = _entry({integration.CONF_HAS_CLOUD_ACCOUNT: True})
    hass = _hass()
    mammotion = SimpleNamespace(
        login_and_initiate_cloud=AsyncMock(
            side_effect=LoginFailedError("account", "bad credentials")
        )
    )
    scheduled = MagicMock()

    with (
        patch.object(
            integration.aiohttp_client,
            "async_get_clientsession",
            return_value=object(),
        ),
        patch.object(integration, "_schedule_cloud_retry", scheduled),
    ):
        result = await integration._async_attempt_login(
            hass,
            entry,
            mammotion,
            "account",
            "password",
            ble_fallback=True,
        )

    assert result is False
    updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data[integration.CONF_HAS_CLOUD_ACCOUNT] is True
    assert integration._cloud_auth_backoff_active(updated_data)
    entry.async_start_reauth.assert_called_once_with(hass)
    scheduled.assert_called_once()


def test_expired_and_invalid_backoff_values_are_inactive() -> None:
    """Only a valid future deadline suppresses a cloud attempt."""
    key = integration.CONF_CLOUD_AUTH_BACKOFF_UNTIL
    assert not integration._cloud_auth_backoff_active({key: "not-a-date"})
    assert not integration._cloud_auth_backoff_active(
        {key: (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    )
    assert integration._cloud_auth_backoff_active(
        {key: (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
    )


@pytest.mark.asyncio
async def test_mower_ble_attachment_uses_latest_advertisement() -> None:
    """Startup forwards both the current BLE device and RSSI to PyMammotion."""
    service_info = SimpleNamespace(device=object(), rssi=-63)
    mower_state = SimpleNamespace(ble_mac=None)
    mammotion = SimpleNamespace(
        get_device_by_name=MagicMock(
            return_value=SimpleNamespace(mower_state=mower_state)
        ),
        update_ble_device=AsyncMock(),
    )
    hass = _hass()
    entry = _entry()
    register_callback = MagicMock()
    integration.bluetooth.async_last_service_info = MagicMock(
        return_value=service_info
    )

    with patch.object(
        integration,
        "_register_ble_reconnect_callback",
        register_callback,
    ):
        await integration._attach_ble_to_mower(
            hass,
            entry,
            mammotion,
            SimpleNamespace(device_name="Yuka-Test"),
            "aa:bb:cc:dd:ee:ff",
        )

    assert mower_state.ble_mac == "aa:bb:cc:dd:ee:ff"
    integration.bluetooth.async_last_service_info.assert_called_once_with(
        hass,
        "AA:BB:CC:DD:EE:FF",
        True,
    )
    mammotion.update_ble_device.assert_awaited_once_with(
        "Yuka-Test",
        service_info.device,
        -63,
    )
    register_callback.assert_called_once()
