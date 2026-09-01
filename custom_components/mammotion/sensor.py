"""Creates the sensor entities for the mower."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfArea,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from pymammotion.data.model.device import (
    MowingDevice,
    PoolCleanerDevice,
    RTKBaseStationDevice,
)
from pymammotion.data.model.enums import RTKStatus
from pymammotion.data.model.pool_state import SpinoSysStatus, SpinoWorkMode
from pymammotion.utility.constant import VioState
from pymammotion.utility.constant.device_constant import (
    AppConnectType,
    PosType,
    RTKPositionMode,
    camera_brightness,
    device_connection,
)
from pymammotion.utility.device_type import DeviceType

from . import MammotionConfigEntry
from .const import DOMAIN
from .coordinator import (
    MAP_SYNC_STATUSES,
    MammotionDeviceErrorUpdateCoordinator,
    MammotionReportUpdateCoordinator,
    MammotionRTKCoordinator,
    MammotionSpinoCoordinator,
)
from .entity import (
    MammotionBaseEntity,
    MammotionBaseRTKEntity,
    MammotionBaseSpinoEntity,
)
from .errors import error_sensor_registry_migration, get_mammotion_error_details


class MowerDataFormatter:
    """Helper class for formatting mower data."""

    @staticmethod
    def parse_time_string(time_str: str) -> time:
        """Convert a minutes-from-midnight string to a time object.

        Args:
            time_str: Integer minutes from midnight as a string (e.g., '1320' for 22:00).

        Returns:
            time object

        """
        if not time_str:
            return time(0, 0)
        try:
            total_minutes = int(time_str)
        except ValueError:
            return time(0, 0)
        return time(total_minutes // 60 % 24, total_minutes % 60)

    @staticmethod
    def format_time(time_str: str) -> str:
        """Convert time string to 12-hour format string.

        Args:
            time_str: Time in format 'HHMM' (e.g., '1330')

        Returns:
            Formatted string (e.g., '01:30pm')

        """
        t = MowerDataFormatter.parse_time_string(time_str)
        return t.strftime("%I:%M%p").lower()

    @staticmethod
    def format_time_range(start: str, end: str) -> str:
        """Format time range from decimal hours."""
        if start == "" or end == "":
            return "Not set"

        return f"{MowerDataFormatter.format_time(start)} - {MowerDataFormatter.format_time(end)}"


@dataclass(frozen=True, kw_only=True)
class MammotionSensorEntityDescription(SensorEntityDescription):
    """Describes Mammotion sensor entity."""

    value_fn: Callable[[MowingDevice], StateType]


@dataclass(frozen=True, kw_only=True)
class MammotionRTKSensorEntityDescription(SensorEntityDescription):
    """Describes Mammotion RTK sensor entity."""

    value_fn: Callable[[RTKBaseStationDevice], StateType]


@dataclass(frozen=True, kw_only=True)
class MammotionSpinoSensorEntityDescription(SensorEntityDescription):
    """Describes Mammotion Spino pool cleaner sensor entity."""

    value_fn: Callable[[PoolCleanerDevice], StateType]


@dataclass(frozen=True, kw_only=True)
class MammotionWorkSensorEntityDescription(SensorEntityDescription):
    """Describes Mammotion sensor entity."""

    value_fn: Callable[[MammotionReportUpdateCoordinator, MowingDevice], StateType]


@dataclass(frozen=True, kw_only=True)
class MammotionSpinoErrorSensorEntityDescription(SensorEntityDescription):
    """Describes a Spino error-log sensor entity."""

    value_fn: Callable[[MammotionSpinoCoordinator], StateType]


LUBA_SENSOR_ONLY_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="blade_height",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        value_fn=lambda mower_data: mower_data.report_data.work.knife_height,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

LUBA_2_YUKA_ONLY_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="camera_brightness",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda mower_data: camera_brightness(
            mower_data.report_data.vision_info.brightness
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="visual_positioning_status",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: VioState(
            mower_data.report_data.vision_info.vio_state
        ).name,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="maintenance_distance",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        value_fn=lambda mower_data: mower_data.report_data.maintenance.mileage,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
    ),
    MammotionSensorEntityDescription(
        key="maintenance_work_time",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda mower_data: mower_data.report_data.maintenance.work_time,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
    ),
    MammotionSensorEntityDescription(
        key="blade_used_time",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda mower_data: mower_data.report_data.maintenance.blade_used_time.blade_used_time,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
    ),
    MammotionSensorEntityDescription(
        key="blade_used_warn_time",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda mower_data: mower_data.report_data.maintenance.blade_used_time.blade_used_warn_time,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
    ),
)

MINI_SERIES_EXCLUDED_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="maintenance_bat_cycles",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda mower_data: mower_data.report_data.maintenance.bat_cycles,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

SENSOR_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="battery_percent",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda mower_data: mower_data.report_data.dev.battery_val,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="ble_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda mower_data: mower_data.report_data.connect.ble_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="wifi_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda mower_data: mower_data.report_data.connect.wifi_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="mnet_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda mower_data: mower_data.report_data.connect.mnet_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="connect_type",
        device_class=SensorDeviceClass.ENUM,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: device_connection(mower_data.report_data.connect),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="gps_stars",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: mower_data.report_data.rtk.gps_stars,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="area",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        value_fn=lambda mower_data: mower_data.report_data.work.area & 65535,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="mowing_speed",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        value_fn=lambda mower_data: mower_data.report_data.work.man_run_speed / 100,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="non_work_hours",
        value_fn=lambda mower_data: MowerDataFormatter.format_time_range(
            mower_data.non_work_hours.start_time,
            mower_data.non_work_hours.end_time,
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="pos_level",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: mower_data.report_data.rtk.pos_level,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="age",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda mower_data: mower_data.report_data.rtk.age,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # MammotionSensorEntityDescription(
    #     key="vlsam_status",
    #     state_class=SensorStateClass.MEASUREMENT,
    #     device_class=None,
    #     native_unit_of_measurement=None,
    #     value_fn=lambda mower_data: (mower_data.report_data.dev.vslam_status & 65280) >> 8,
    # ),
    MammotionSensorEntityDescription(
        key="positioning_mode",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: str(
            RTKStatus.from_value(mower_data.report_data.rtk.status)
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="position_mode",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda mower_data: RTKPositionMode(
            mower_data.report_data.basestation_info.rtk_status
        ).name,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="position_type",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: str(
            PosType(mower_data.location.position_type).name
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="rtk_latitude",
        native_unit_of_measurement=DEGREE,
        value_fn=lambda mower_data: mower_data.location.RTK.latitude * 180.0 / math.pi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="rtk_longitude",
        native_unit_of_measurement=DEGREE,
        value_fn=lambda mower_data: mower_data.location.RTK.longitude * 180.0 / math.pi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # MammotionSensorEntityDescription(
    #     key="lawn_mower_position",
    #     state_class=None,
    #     device_class=None,  # Set device class to "geo_location"
    #     native_unit_of_measurement=None,
    #     value_fn=lambda mower_data: f"{mower_data.location.device.latitude}, {mower_data.location.device.longitude}"
    # )
    # ToDo: We still need to add the following.
    # - RTK Status - None, Single, Fix, Float, Unknown (RTKStatusFragment.java)
    # - Signal quality (Robot)
    # - Signal quality (Ref. Station)
    # - LoRa number
    # - WiFi status
    # 'real_pos_x': -142511, 'real_pos_y': -20548, 'real_toward': 50915, (robot position)
)

# Luba 2 / Yuka (non-RTK) only — APK refreshNonRtkDeviceUI shows these;
# Luba 1 and standard RTK devices do not display them.
LUBA_2_YUKA_SIGNAL_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="l1_satellites",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.dis_status >> 16) & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="l2_satellites",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.dis_status >> 24) & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="co_view_l1",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: mower_data.report_data.rtk.co_view_stars & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="co_view_l2",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.co_view_stars >> 8)
        & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="rtk_signal",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.dis_status >> 40) & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSensorEntityDescription(
        key="device_signal",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.dis_status >> 32) & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

# Luba 1 only — APK refreshLuba1ModeUI shows base_link_status (connection_to_ref);
# Luba 2 / Yuka and RTK devices hide it.
LUBA_1_SIGNAL_TYPES: tuple[MammotionSensorEntityDescription, ...] = (
    MammotionSensorEntityDescription(
        key="base_link_status",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda mower_data: (mower_data.report_data.rtk.dis_status >> 48) & 255,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

WORK_SENSOR_TYPES: tuple[MammotionWorkSensorEntityDescription, ...] = (
    MammotionWorkSensorEntityDescription(
        key="map_sync_status",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        options=list(MAP_SYNC_STATUSES),
        native_unit_of_measurement=None,
        value_fn=lambda coordinator, mower_data: coordinator.map_sync_status,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionWorkSensorEntityDescription(
        key="mqtt_status",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        options=["reported_online", "reported_offline"],
        native_unit_of_measurement=None,
        value_fn=lambda coordinator, mower_data: (
            "reported_online" if coordinator.mqtt_device_online else "reported_offline"
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

RTK_SENSOR_TYPES: tuple[MammotionRTKSensorEntityDescription, ...] = (
    MammotionRTKSensorEntityDescription(
        key="rtk_lora",
        value_fn=lambda rtk_data: rtk_data.lora_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="rtk_latitude",
        native_unit_of_measurement=DEGREE,
        value_fn=lambda rtk_data: rtk_data.lat * 180 / math.pi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="rtk_longitude",
        native_unit_of_measurement=DEGREE,
        value_fn=lambda rtk_data: rtk_data.lon * 180 / math.pi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="rtk_wifi_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda rtk_data: rtk_data.wifi_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="rtk_sats_num",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda rtk_data: rtk_data.sats_num,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="position_mode",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda rtk_data: RTKPositionMode(rtk_data.rtk_status).name,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionRTKSensorEntityDescription(
        key="rtk_app_connect_type",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda rtk_data: AppConnectType(rtk_data.app_connect_type).name,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

SPINO_SENSOR_TYPES: tuple[MammotionSpinoSensorEntityDescription, ...] = (
    MammotionSpinoSensorEntityDescription(
        key="spino_battery",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda spino_data: spino_data.pool_state.battery,
    ),
    MammotionSpinoSensorEntityDescription(
        key="spino_status",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        options=[status.name for status in SpinoSysStatus],
        value_fn=lambda spino_data: spino_data.pool_state.sys_status.name,
    ),
    MammotionSpinoSensorEntityDescription(
        key="spino_work_mode",
        state_class=None,
        device_class=SensorDeviceClass.ENUM,
        options=[mode.name for mode in SpinoWorkMode],
        value_fn=lambda spino_data: spino_data.pool_state.work_mode.name,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSpinoSensorEntityDescription(
        key="spino_ble_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda spino_data: spino_data.pool_state.ble_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSpinoSensorEntityDescription(
        key="spino_wifi_rssi",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda spino_data: spino_data.pool_state.wifi_rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

SPINO_ERROR_SENSOR_TYPES: tuple[MammotionSpinoErrorSensorEntityDescription, ...] = (
    MammotionSpinoErrorSensorEntityDescription(
        key="spino_error_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.get_error_time(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSpinoErrorSensorEntityDescription(
        key="spino_error_message",
        state_class=None,
        native_unit_of_measurement=None,
        device_class=None,
        value_fn=lambda coordinator: (
            msg[:255] if (msg := coordinator.get_error_message()) is not None else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSpinoErrorSensorEntityDescription(
        key="spino_error_code",
        state_class=None,
        native_unit_of_measurement=None,
        device_class=None,
        value_fn=lambda coordinator: coordinator.get_error_code(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionSpinoErrorSensorEntityDescription(
        key="spino_mqtt_status",
        state_class=None,
        native_unit_of_measurement=None,
        device_class=SensorDeviceClass.ENUM,
        options=["online", "offline"],
        value_fn=lambda coordinator: "online"
        if coordinator.mqtt_device_online
        else "offline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def _migrate_retired_error_sensors(
    hass: HomeAssistant,
    config_entry_id: str,
    unique_name: str,
) -> None:
    """Preserve the code entity ID and remove superseded error fields."""
    registry = er.async_get(hass)
    entries = [
        registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            registry, config_entry_id
        )
        if registry_entry.domain == SENSOR_DOMAIN
        and registry_entry.platform == DOMAIN
    ]
    entries_by_unique_id = {entry.unique_id: entry for entry in entries}
    migration_source, retired_unique_ids = error_sensor_registry_migration(
        unique_name,
        set(entries_by_unique_id),
    )
    if migration_source is not None:
        registry.async_update_entity(
            entries_by_unique_id[migration_source].entity_id,
            new_unique_id=f"{unique_name}_latest_error",
        )
    for retired_unique_id in retired_unique_ids:
        registry.async_remove(entries_by_unique_id[retired_unique_id].entity_id)


_PRIMARY_MOWER_SENSOR_DUPLICATES = {
    "activity_mode",
    "elapsed_time",
    "left_time",
    "progress",
    "total_time",
    "work_area",
}


def _cleanup_primary_mower_sensor_duplicates(
    hass: HomeAssistant,
    config_entry_id: str,
    unique_name: str,
) -> None:
    """Remove registry entries now represented by the primary mower entity."""
    registry = er.async_get(hass)
    exact_unique_ids = {
        f"{unique_name}_{key}" for key in _PRIMARY_MOWER_SENSOR_DUPLICATES
    }
    task_area_prefix = f"{unique_name}_"
    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        if entry.domain != SENSOR_DOMAIN or entry.platform != DOMAIN:
            continue
        if entry.unique_id in exact_unique_ids or (
            entry.unique_id.startswith(task_area_prefix)
            and entry.unique_id.endswith("_task_area")
        ):
            registry.async_remove(entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor platform."""
    mammotion_mowers = entry.runtime_data.mowers

    entities = []
    for mower in mammotion_mowers:
        _migrate_retired_error_sensors(
            hass,
            entry.entry_id,
            mower.reporting_coordinator.unique_name,
        )
        _cleanup_primary_mower_sensor_duplicates(
            hass,
            entry.entry_id,
            mower.reporting_coordinator.unique_name,
        )
        if not DeviceType.is_yuka(mower.device.device_name):
            entities.extend(
                MammotionSensorEntity(mower.reporting_coordinator, description)
                for description in LUBA_SENSOR_ONLY_TYPES
            )

        if DeviceType.is_luba_pro(mower.device.device_name):
            entities.extend(
                MammotionSensorEntity(mower.reporting_coordinator, description)
                for description in LUBA_2_YUKA_ONLY_TYPES
            )
            entities.extend(
                MammotionSensorEntity(mower.reporting_coordinator, description)
                for description in LUBA_2_YUKA_SIGNAL_TYPES
            )
            device_type = DeviceType.value_of_str(
                mower.device.device_name, mower.device.product_key
            )
            if device_type.supports_battery_cycle_count():
                entities.extend(
                    MammotionSensorEntity(mower.reporting_coordinator, description)
                    for description in MINI_SERIES_EXCLUDED_TYPES
                )

        if DeviceType.is_luba1(mower.device.device_name, mower.device.product_key):
            entities.extend(
                MammotionSensorEntity(mower.reporting_coordinator, description)
                for description in LUBA_1_SIGNAL_TYPES
            )

        entities.extend(
            MammotionSensorEntity(mower.reporting_coordinator, description)
            for description in SENSOR_TYPES
        )
        entities.extend(
            MammotionWorkSensorEntity(mower.reporting_coordinator, description)
            for description in WORK_SENSOR_TYPES
        )

        entities.append(MammotionErrorSensorEntity(mower.error_coordinator))


    mammotion_rtks = entry.runtime_data.RTK
    for rtk in mammotion_rtks:
        entities.extend(
            MammotionRTKSensorEntity(rtk.coordinator, description)
            for description in RTK_SENSOR_TYPES
        )

    mammotion_spinos = entry.runtime_data.spino
    for spino in mammotion_spinos:
        entities.extend(
            MammotionSpinoSensorEntity(spino.coordinator, description)
            for description in SPINO_SENSOR_TYPES
        )
        entities.extend(
            MammotionSpinoErrorSensorEntity(spino.coordinator, description)
            for description in SPINO_ERROR_SENSOR_TYPES
        )

    async_add_entities(entities)


class MammotionSensorEntity(MammotionBaseEntity, SensorEntity):
    """Defining the Mammotion Sensor."""

    entity_description: MammotionSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MammotionReportUpdateCoordinator,
        entity_description: MammotionSensorEntityDescription,
    ) -> None:
        """Set up MammotionSensor."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)


class MammotionRTKSensorEntity(MammotionBaseRTKEntity, SensorEntity):
    """Defining the Mammotion Sensor."""

    entity_description: MammotionRTKSensorEntityDescription

    def __init__(
        self,
        coordinator: MammotionRTKCoordinator,
        entity_description: MammotionRTKSensorEntityDescription,
    ) -> None:
        """Set up MammotionSensor."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)


class MammotionSpinoSensorEntity(MammotionBaseSpinoEntity, SensorEntity):
    """Defining the Mammotion Spino pool cleaner Sensor."""

    entity_description: MammotionSpinoSensorEntityDescription

    def __init__(
        self,
        coordinator: MammotionSpinoCoordinator,
        entity_description: MammotionSpinoSensorEntityDescription,
    ) -> None:
        """Set up MammotionSpinoSensor."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)


class MammotionSpinoErrorSensorEntity(MammotionBaseSpinoEntity, SensorEntity):
    """Sensor entity for a single Spino error-log field (code, time, or message)."""

    entity_description: MammotionSpinoErrorSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MammotionSpinoCoordinator,
        entity_description: MammotionSpinoErrorSensorEntityDescription,
    ) -> None:
        """Set up MammotionSpinoErrorSensorEntity."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator)


class MammotionErrorSensorEntity(MammotionBaseEntity, SensorEntity):
    """Expose the latest mower error as one diagnostic entity."""

    _attr_has_entity_name = True
    _attr_translation_key = "latest_error"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MammotionDeviceErrorUpdateCoordinator,
    ) -> None:
        """Set up the latest error sensor."""
        super().__init__(coordinator, "latest_error")

    @property
    def native_value(self) -> StateType:
        """Return the latest error code."""
        details = get_mammotion_error_details(
            self.coordinator.data,
            self.coordinator.hass.config.language,
        )
        return details.code if details is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return details for the latest historical error."""
        details = get_mammotion_error_details(
            self.coordinator.data,
            self.coordinator.hass.config.language,
        )
        if details is None:
            return None
        return {
            "occurred_at": details.occurred_at,
            "message": details.message,
            "level": details.level,
        }


class MammotionWorkSensorEntity(MammotionBaseEntity, SensorEntity):
    """Defining the Mammotion Sensor."""

    entity_description: MammotionWorkSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MammotionReportUpdateCoordinator,
        entity_description: MammotionWorkSensorEntityDescription,
    ) -> None:
        """Set up MammotionSensor."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator, self.coordinator.data)
