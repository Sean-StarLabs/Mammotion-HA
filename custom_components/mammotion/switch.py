"""Support for Mammotion switches."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from functools import partial
from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify
from pymammotion.data.model.device import MowingDevice, PoolCleanerDevice
from pymammotion.data.model.pool_state import SpinoToggle
from pymammotion.utility.device_type import DeviceType

from . import MammotionConfigEntry
from .const import DOMAIN, LOGGER
from .coordinator import (
    MammotionBaseUpdateCoordinator,
    MammotionReportUpdateCoordinator,
    MammotionSpinoCoordinator,
    is_active_mow_task,
)
from .entity import MammotionBaseEntity, MammotionBaseSpinoEntity
from .yuka import is_yuka_2

# Matches pymammotion's auto-generated fallback names ("area 1", "area 2", …).
# These carry no user intent and must be treated the same as empty names.
_PYMAMMOTION_AUTO_NAME = re.compile(r"^area\s+\d+$", re.IGNORECASE)


def _next_task_setting_available(
    coordinator: MammotionBaseUpdateCoordinator,
) -> bool:
    """Return whether a route setting can be changed for the next task."""
    mower_data = coordinator.data
    return not isinstance(mower_data, MowingDevice) or not is_active_mow_task(
        mower_data
    )


@dataclass(frozen=True, kw_only=True)
class MammotionSwitchEntityDescription(SwitchEntityDescription):
    """Describes Mammotion switch entity."""

    key: str


@dataclass(frozen=True, kw_only=True)
class MammotionAsyncSwitchEntityDescription(MammotionSwitchEntityDescription):
    """Describes Mammotion switch entity."""

    is_on_func: Callable[[MammotionBaseUpdateCoordinator], bool] | None = None
    always_available: bool = False
    restore_last_state: bool = False
    set_fn: Callable[[MammotionBaseUpdateCoordinator, bool], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class MammotionConfigSwitchEntityDescription(MammotionSwitchEntityDescription):
    """Describes Mammotion Config switch entity."""

    available_fn: Callable[[MammotionBaseUpdateCoordinator], bool] | None = None
    device_task_setting: bool = False
    set_fn: Callable[[MammotionBaseUpdateCoordinator, bool], None]


@dataclass(frozen=True, kw_only=True)
class MammotionConfigAreaSwitchEntityDescription(MammotionSwitchEntityDescription):
    """Describes the Areas entities."""

    area: int
    set_fn: Callable[[MammotionBaseUpdateCoordinator, bool, int], None]


@dataclass(frozen=True, kw_only=True)
class MammotionSpinoSwitchEntityDescription(SwitchEntityDescription):
    """Describes a Mammotion Spino pool cleaner switch entity."""

    key: str
    is_on_fn: Callable[[PoolCleanerDevice], bool]
    set_fn: Callable[[MammotionSpinoCoordinator, bool], Awaitable[None]]


SPINO_SWITCH_ENTITIES: tuple[MammotionSpinoSwitchEntityDescription, ...] = (
    MammotionSpinoSwitchEntityDescription(
        key="spino_buzzer",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda spino_data: spino_data.pool_state.buzzer,
        set_fn=lambda coordinator, value: coordinator.async_set_pool_toggle(
            SpinoToggle.buzzer, value
        ),
    ),
    MammotionSpinoSwitchEntityDescription(
        key="spino_turbo_clean",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda spino_data: spino_data.pool_state.turbo_clean,
        set_fn=lambda coordinator, value: coordinator.async_set_pool_toggle(
            SpinoToggle.turbo_clean, value
        ),
    ),
    MammotionSpinoSwitchEntityDescription(
        key="spino_platform_cleaning",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda spino_data: spino_data.pool_state.platform_cleaning,
        set_fn=lambda coordinator, value: coordinator.async_set_pool_toggle(
            SpinoToggle.platform_cleaning, value
        ),
    ),
    MammotionSpinoSwitchEntityDescription(
        key="spino_waterline_parking",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda spino_data: spino_data.pool_state.waterline_parking,
        set_fn=lambda coordinator, value: coordinator.async_set_pool_toggle(
            SpinoToggle.waterline_parking, value
        ),
    ),
)


YUKA_CONFIG_SWITCH_ENTITIES: tuple[MammotionConfigSwitchEntityDescription, ...] = (
    MammotionConfigSwitchEntityDescription(
        key="is_mow",
        set_fn=lambda coordinator, value: setattr(
            coordinator.operation_settings, "is_mow", value
        ),
    ),
    MammotionConfigSwitchEntityDescription(
        key="is_dump",
        set_fn=lambda coordinator, value: setattr(
            coordinator.operation_settings, "is_dump", value
        ),
    ),
    MammotionConfigSwitchEntityDescription(
        key="is_edge",
        set_fn=lambda coordinator, value: setattr(
            coordinator.operation_settings, "is_edge", value
        ),
    ),
)

MINI_AND_X_SERIES_CONFIG_SWITCH_ENTITIES: tuple[
    MammotionAsyncSwitchEntityDescription, ...
] = (
    MammotionAsyncSwitchEntityDescription(
        key="manual_light",
        is_on_func=lambda coordinator: coordinator.data.mower_state.lamp_info.manual_light,
        set_fn=lambda coordinator, value: coordinator.async_set_manual_light(value),
    ),
    MammotionAsyncSwitchEntityDescription(
        key="night_light",
        is_on_func=lambda coordinator: coordinator.data.mower_state.lamp_info.night_light,
        set_fn=lambda coordinator, value: coordinator.async_set_night_light(value),
    ),
)

YUKA_2_EXTRA_SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="auto_lighting",
        is_on_func=lambda coordinator: coordinator.data.mower_state.lamp_info.night_light,
        set_fn=lambda coordinator, value: coordinator.async_set_night_light(value),
        entity_category=EntityCategory.CONFIG,
    ),
)

AUDIO_SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="voice_on_off",
        is_on_func=lambda coordinator: coordinator.data.mower_state.audio.volume > 0,
        set_fn=lambda coordinator, value: coordinator.async_set_voice_on_off(value),
        entity_category=EntityCategory.CONFIG,
    ),
)

SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="side_led",
        is_on_func=lambda coordinator: coordinator.data.mower_state.side_led.enable
        == 0,
        set_fn=lambda coordinator, value: coordinator.async_set_sidelight(int(value)),
        entity_category=EntityCategory.CONFIG,
    ),
    MammotionAsyncSwitchEntityDescription(
        key="rain_detection",
        is_on_func=lambda coordinator: coordinator.data.mower_state.rain_detection,
        set_fn=lambda coordinator, value: coordinator.async_set_rain_detection(value),
        entity_category=EntityCategory.CONFIG,
    ),
)

LUBA_1_SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="blade_status",
        set_fn=lambda coordinator, value: coordinator.async_start_stop_blades(value),
        is_on_func=lambda coordinator: coordinator.data.mower_state.blade_status,
    ),
)

UPDATE_SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="schedule_updates",
        is_on_func=lambda coordinator: coordinator.data.enabled,
        set_fn=lambda coordinator, value: coordinator.set_scheduled_updates(value),
    ),
)

CONNECTIVITY_SWITCH_ENTITIES: tuple[MammotionAsyncSwitchEntityDescription, ...] = (
    MammotionAsyncSwitchEntityDescription(
        key="bluetooth_enabled",
        is_on_func=lambda coordinator: coordinator.bluetooth_enabled,
        set_fn=lambda coordinator, value: coordinator.async_set_bluetooth_enabled(
            value
        ),
        restore_last_state=True,
        always_available=True,
        entity_category=EntityCategory.CONFIG,
    ),
    MammotionAsyncSwitchEntityDescription(
        key="cloud_enabled",
        is_on_func=lambda coordinator: coordinator.cloud_enabled,
        set_fn=lambda coordinator, value: coordinator.async_set_cloud_enabled(value),
        always_available=True,
        entity_category=EntityCategory.CONFIG,
    ),
)

CONFIG_SWITCH_ENTITIES: tuple[MammotionConfigSwitchEntityDescription, ...] = (
    MammotionConfigSwitchEntityDescription(
        key="rain_tactics",
        device_task_setting=True,
        set_fn=lambda coordinator, value: setattr(
            coordinator.operation_settings, "rain_tactics", int(value)
        ),
        available_fn=_next_task_setting_available,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Mammotion switch entities."""
    mammotion_devices = entry.runtime_data.mowers

    for mower in mammotion_devices:
        _cleanup_removed_yuka_2_switches(hass, mower.device.device_name)
        _cleanup_cloud_switch_without_account(
            hass,
            mower.unique_name,
            mower.reporting_coordinator.has_cloud_account,
        )
        added_areas: set[int] = set()
        area_entities_by_hash: dict[int, MammotionConfigAreaSwitchEntity] = {}
        coordinator = mower.reporting_coordinator

        update_areas = partial(
            async_add_area_entities,
            coordinator,
            added_areas,
            area_entities_by_hash,
            async_add_entities,
        )

        update_areas()
        coordinator.subscribe_map_updated(update_areas)

        device_name = mower.device.device_name
        entities: list = [
            MammotionSwitchEntity(coordinator, d) for d in SWITCH_ENTITIES
        ]

        if DeviceType.is_luba_pro(device_name) or is_yuka_2(device_name):
            entities.extend(
                MammotionSwitchEntity(coordinator, d) for d in AUDIO_SWITCH_ENTITIES
            )

        entities.extend(
            MammotionUpdateSwitchEntity(coordinator, d)
            for d in UPDATE_SWITCH_ENTITIES
        )
        entities.extend(
            MammotionSwitchEntity(coordinator, d)
            for d in CONNECTIVITY_SWITCH_ENTITIES
            if d.key != "cloud_enabled" or coordinator.has_cloud_account
        )

        if is_yuka_2(device_name):
            entities.extend(
                MammotionSwitchEntity(coordinator, d)
                for d in YUKA_2_EXTRA_SWITCH_ENTITIES
            )
            async_add_entities(entities)
            continue

        entities.extend(
            MammotionConfigSwitchEntity(coordinator, d) for d in CONFIG_SWITCH_ENTITIES
        )
        if DeviceType.is_yuka(device_name) and not DeviceType.is_yuka_mini(device_name):
            entities.extend(
                MammotionConfigSwitchEntity(coordinator, d)
                for d in YUKA_CONFIG_SWITCH_ENTITIES
            )

        if DeviceType.is_luba1(device_name):
            entities.extend(
                MammotionSwitchEntity(coordinator, d) for d in LUBA_1_SWITCH_ENTITIES
            )

        if DeviceType.is_mini_or_x_series(device_name):
            entities.extend(
                MammotionSwitchEntity(coordinator, d)
                for d in MINI_AND_X_SERIES_CONFIG_SWITCH_ENTITIES
            )

        async_add_entities(entities)

    for spino in entry.runtime_data.spino:
        async_add_entities(
            MammotionSpinoSwitchEntity(spino.coordinator, entity_description)
            for entity_description in SPINO_SWITCH_ENTITIES
        )


class MammotionSwitchEntity(MammotionBaseEntity, SwitchEntity, RestoreEntity):
    """Mammotion switch entity."""

    entity_description: MammotionAsyncSwitchEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MammotionBaseUpdateCoordinator,
        entity_description: MammotionAsyncSwitchEntityDescription,
    ) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator, entity_description.key)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key
        if callable(entity_description.is_on_func):
            self._attr_is_on = entity_description.is_on_func(self.coordinator)
        else:
            self._attr_is_on = False  # Default state

    @property
    def available(self) -> bool:
        """Keep local transport policy controls available while offline."""
        if self.entity_description.always_available:
            return True
        return super().available

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        self._attr_is_on = True
        self.async_write_ha_state()
        try:
            await self.entity_description.set_fn(self.coordinator, True)
        except Exception:
            self._attr_is_on = False
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        self.async_write_ha_state()
        try:
            await self.entity_description.set_fn(self.coordinator, False)
        except Exception:
            self._attr_is_on = True
            self.async_write_ha_state()
            raise

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if callable(self.entity_description.is_on_func):
            self._attr_is_on = self.entity_description.is_on_func(self.coordinator)
        super()._handle_coordinator_update()

    async def async_update(self) -> None:
        """Update the entity state."""
        if self.entity_description.is_on_func is not None:
            self._attr_is_on = self.entity_description.is_on_func(self.coordinator)
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if (
            self.entity_description.is_on_func is not None
            and not self.entity_description.restore_last_state
        ):
            return
        if not (last_state := await self.async_get_last_state()):
            return
        restored_state = last_state.state == STATE_ON
        if self.entity_description.restore_last_state:
            try:
                await self.entity_description.set_fn(
                    self.coordinator, restored_state
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Restored %s policy for %s but could not apply its transport: %s",
                    self.entity_description.key,
                    self.coordinator.device_name,
                    exc,
                )
            self._attr_is_on = restored_state
            self.async_write_ha_state()
        else:
            self._attr_is_on = restored_state


class MammotionUpdateSwitchEntity(MammotionBaseEntity, SwitchEntity, RestoreEntity):
    """Mammotion switch entity for controlling scheduled updates."""

    entity_description: MammotionAsyncSwitchEntityDescription
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MammotionBaseUpdateCoordinator,
        entity_description: MammotionAsyncSwitchEntityDescription,
    ) -> None:
        """Initialize the update switch entity."""
        super().__init__(coordinator, entity_description.key)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key
        self._attr_is_on = True  # Default state

    @property
    def is_on(self) -> bool:
        """Return if settings is on or off."""
        if self.entity_description.is_on_func is not None:
            return self.entity_description.is_on_func(self.coordinator)
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.entity_description.set_fn(self.coordinator, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.entity_description.set_fn(self.coordinator, False)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update the entity state."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON


class MammotionConfigSwitchEntity(MammotionBaseEntity, SwitchEntity, RestoreEntity):
    """Mammotion config switch entity."""

    entity_description: MammotionConfigSwitchEntityDescription
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MammotionBaseUpdateCoordinator,
        entity_description: MammotionConfigSwitchEntityDescription,
    ) -> None:
        """Initialize the config switch entities."""
        super().__init__(coordinator, entity_description.key)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def is_on(self) -> bool:
        """Return if settings is on or off."""
        return getattr(
            self.coordinator.operation_settings, self.entity_description.key, False
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        self._attr_is_on = True
        self.entity_description.set_fn(self.coordinator, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        self.entity_description.set_fn(self.coordinator, False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if (
            self.entity_description.device_task_setting
            and self.coordinator.operation_settings_from_device
        ):
            return
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON
        self.entity_description.set_fn(self.coordinator, self._attr_is_on)

    async def async_update(self) -> None:
        """Update the entity state."""

    @property
    def available(self) -> bool:
        """Return True when this switch applies to the current task settings."""
        if not super().available:
            return False
        if (
            self.entity_description.device_task_setting
            and not self.coordinator.route_task_settings_available
        ):
            return False
        return self.entity_description.available_fn is None or (
            self.entity_description.available_fn(self.coordinator)
        )


class MammotionConfigAreaSwitchEntity(MammotionBaseEntity, SwitchEntity, RestoreEntity):
    """Mammotion Config Area Switch Entity."""

    entity_description: MammotionConfigAreaSwitchEntityDescription
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MammotionBaseUpdateCoordinator,
        entity_description: MammotionConfigAreaSwitchEntityDescription,
    ) -> None:
        """Initialize the area switch entity."""
        super().__init__(coordinator, entity_description.key)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.area = entity_description.area
        self._attr_extra_state_attributes = {"hash": self.area}
        self._attr_is_on = self.area in self.coordinator.operation_settings.areas
        # Last custom name we pushed to the device, so an unrelated registry
        # update (icon, area assignment, …) doesn't re-send set_area_name.
        self._pushed_name: str | None = None

    def update_name(self, new_name: str) -> None:
        """Update the display name when the device provides a real name for this area."""
        self.entity_description = dataclass_replace(
            self.entity_description,
            name=new_name,
            translation_placeholders={"name": new_name},
        )
        # Don't overwrite _pushed_name when the user has set their own HA label —
        # resetting it to a device/auto name would cause a spurious set_area_name
        # push the next time async_registry_entry_updated fires.
        registry_entry = getattr(self, "registry_entry", None)
        if not (registry_entry and registry_entry.name):
            self._pushed_name = new_name
        if self.hass is not None:
            self.async_write_ha_state()

    def update_area(self, new_area_id: int) -> None:
        """Update the area hash when the device reports a new hash for the same named area."""
        old_area = self.area
        new_unique_id = f"{self.coordinator.unique_name}_{new_area_id}"
        if self.hass is not None and self.entity_id:
            registry = er.async_get(self.hass)
            if conflicting_entity_id := registry.async_get_entity_id(
                SWITCH_DOMAIN, DOMAIN, new_unique_id
            ):
                if conflicting_entity_id != self.entity_id:
                    registry.async_remove(conflicting_entity_id)
            registry.async_update_entity(
                self.entity_id, new_unique_id=new_unique_id
            )
        self._attr_unique_id = new_unique_id
        self.area = new_area_id
        self._attr_extra_state_attributes = {"hash": new_area_id}
        if old_area in self.coordinator.operation_settings.areas:
            self.coordinator.operation_settings.areas.remove(old_area)
            if new_area_id not in self.coordinator.operation_settings.areas:
                self.coordinator.operation_settings.areas.append(new_area_id)
        self._attr_is_on = new_area_id in self.coordinator.operation_settings.areas
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        self._attr_is_on = True
        self.entity_description.set_fn(self.coordinator, True, self.area)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        self.entity_description.set_fn(self.coordinator, False, self.area)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        await super().async_added_to_hass()
        # Seed with any existing name override so we only push live user edits.
        self._pushed_name = self.registry_entry.name if self.registry_entry else None
        if self.coordinator.operation_settings_from_device:
            return
        last_state = await self.async_get_last_state()
        if last_state and last_state.state == STATE_ON:
            await self.async_turn_on()

    @callback
    def async_registry_entry_updated(self) -> None:
        """Push a user-edited entity name to the device as this area's name."""
        super().async_registry_entry_updated()
        # Pushing area names back to the device is only supported on Luba Pro
        # (Luba 2) and newer models.
        if not DeviceType.is_luba_pro(self.coordinator.device_name):
            return
        if self.registry_entry:
            if new_name := self.registry_entry.name:
                if new_name == self._pushed_name:
                    return
                self._pushed_name = new_name
                self.hass.async_create_task(
                    self.coordinator.async_set_area_name(self.area, new_name)
                )

    async def async_update(self) -> None:
        """Update the entity state."""
        self._attr_is_on = self.area in self.coordinator.operation_settings.areas
        known_area_hashes = _known_area_hashes(self.coordinator)
        area_identity_loaded = _area_names_loaded(
            self.coordinator
        ) or DeviceType.is_luba1(self.coordinator.device_name)
        if self.area not in known_area_hashes and area_identity_loaded:
            await self.async_remove()
            return
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True


@callback
def async_add_area_entities(
    coordinator: MammotionReportUpdateCoordinator,
    added_areas: set[int],
    area_entities_by_hash: dict[int, MammotionConfigAreaSwitchEntity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Handle addition of mowing areas."""
    if coordinator.data is None:
        return

    computed = coordinator.data.map.computed_areas or []
    map_area_hashes: set[int] = {
        area_id
        for k in coordinator.data.map.area
        if str(k).lstrip("-").isdigit() and (area_id := int(k)) != 0
    }
    area_names_by_hash = _active_named_areas(coordinator, map_area_hashes)
    real_named_map_area_hashes = set(area_names_by_hash)
    area_names = {name.lower() for name in area_names_by_hash.values()}
    for area in computed:
        raw_area_name = str(getattr(area, "name", "") or "").strip()
        area_name = _display_area_name(area.hash, raw_area_name)
        if (
            area.hash in area_names_by_hash
            and _is_generic_area_name(area_name, area.hash)
        ):
            continue
        if map_area_hashes and area.hash not in map_area_hashes:
            if area_name.lower() in area_names:
                continue
        area_names_by_hash[area.hash] = area_name
        area_names.add(area_name.lower())
        if raw_area_name and not _is_generic_area_name(raw_area_name, area.hash):
            real_named_map_area_hashes.add(area.hash)
    area_names_by_hash.update(_fallback_named_areas(coordinator, area_names_by_hash))
    for area_id in sorted(map_area_hashes):
        area_names_by_hash.setdefault(area_id, _display_area_name(area_id, ""))
    all_current_areas = set(area_names_by_hash)
    if map_area_hashes and not all_current_areas:
        return

    # Trigger re-fetch when the device hasn't yet sent names for all areas.
    # Check before considering generated fallbacks, which are selectable but do
    # not prove that the device's name list has arrived.
    if not DeviceType.is_luba1(coordinator.device_name):
        if map_area_hashes - real_named_map_area_hashes:
            coordinator.hass.async_create_task(coordinator.async_get_area_list())

    # Startup registry cleanup: remove stale entries from previous sessions.
    area_names_loaded = _area_names_loaded(coordinator)
    area_identity_loaded = area_names_loaded or DeviceType.is_luba1(
        coordinator.device_name
    )
    if map_area_hashes and area_identity_loaded:
        _async_migrate_named_area_registry_entries(coordinator, area_names_by_hash)

    # Early exit when neither the set of area hashes nor any name has changed.
    if all_current_areas == added_areas:
        if all(
            area_id in area_entities_by_hash
            and area_entities_by_hash[area_id].entity_description.name == name
            for area_id, name in area_names_by_hash.items()
        ):
            if map_area_hashes and area_identity_loaded:
                _async_clean_stale_area_registry_entries(
                    coordinator, all_current_areas, area_names_by_hash
                )
            return

    def set_area_entity(
        coord: MammotionReportUpdateCoordinator, bool_val: bool, value: int
    ) -> None:
        if bool_val:
            if value not in coord.operation_settings.areas:
                coord.operation_settings.areas.append(value)
        elif value in coord.operation_settings.areas:
            coord.operation_settings.areas.remove(value)

    switch_entities: list[MammotionConfigAreaSwitchEntity] = []
    for area_id, new_name in sorted(
        area_names_by_hash.items(), key=lambda item: item[1].lower()
    ):

        if area_id in added_areas:
            # Already tracked — update name unless we'd overwrite a real device name
            # with an auto-generated one (protects user-visible names from renumbering).
            if area_id in area_entities_by_hash:
                entity = area_entities_by_hash[area_id]
                current_name = entity.entity_description.name or ""
                if current_name != new_name:
                    is_new_auto = bool(_PYMAMMOTION_AUTO_NAME.match(new_name))
                    is_cur_auto = bool(_PYMAMMOTION_AUTO_NAME.match(current_name))
                    if not (is_new_auto and not is_cur_auto):
                        entity.update_name(new_name)
            continue

        # Not yet tracked — for real (non-auto) names, update the existing entity's
        # hash only if that name's old hash has disappeared from the current map.
        existing = next(
            (
                entity
                for old_hash, entity in area_entities_by_hash.items()
                if area_id in map_area_hashes
                and old_hash not in map_area_hashes
                and entity.entity_description.name == new_name
            ),
            None,
        )
        if not _PYMAMMOTION_AUTO_NAME.match(new_name) and existing is not None:
            old_hash = existing.area
            added_areas.discard(existing.area)
            existing.update_area(area_id)
            area_entities_by_hash.pop(old_hash, None)
            area_entities_by_hash[area_id] = existing
            added_areas.add(area_id)
            continue

        # Missing area — add a new entity with the name supplied by computed_areas.
        base_area_switch_entity = MammotionConfigAreaSwitchEntityDescription(
            key=_area_entity_key(area_id, new_name),
            translation_key="area",
            translation_placeholders={"name": new_name},
            area=area_id,
            name=new_name,
            set_fn=set_area_entity,
        )
        entity = MammotionConfigAreaSwitchEntity(coordinator, base_area_switch_entity)
        switch_entities.append(entity)
        area_entities_by_hash[area_id] = entity
        added_areas.add(area_id)

    if map_area_hashes and area_identity_loaded:
        _async_clean_stale_area_registry_entries(
            coordinator, all_current_areas, area_names_by_hash
        )

    # Guard: only remove when map.area is non-empty — an empty map is a transient
    # refresh state and must not wipe the entity registry.
    if map_area_hashes and area_identity_loaded:
        old_areas = added_areas - all_current_areas
        if old_areas:
            async_remove_stale_area_entities(coordinator, old_areas)
            for area in old_areas:
                added_areas.discard(area)
                area_entities_by_hash.pop(area, None)

    if switch_entities:
        async_add_entities(switch_entities)


def _area_entity_key(area_id: int, _name: str) -> str:
    """Return a stable key for an area entity."""
    return f"{area_id}"


def _display_area_name(area_id: int, name: str) -> str:
    """Return a user-facing name for an area, preserving real device names."""
    if not name or _is_generic_area_name(name, area_id):
        return f"area {area_id}"
    return name


def _active_named_areas(
    coordinator: MammotionReportUpdateCoordinator, map_area_hashes: set[int]
) -> dict[int, str]:
    """Return real area names keyed by active mower area hash."""
    if not map_area_hashes:
        return {}

    named_areas: dict[int, str] = {}
    for area_hash, area_data in coordinator.data.map.area.items():
        try:
            area_id = int(area_hash)
        except (TypeError, ValueError):
            continue
        if area_id not in map_area_hashes:
            continue
        for frame in getattr(area_data, "data", []) or []:
            name_time = getattr(frame, "name_time", None)
            name = str(getattr(name_time, "name", "") or "").strip()
            if name and not _is_generic_area_name(name, area_id):
                named_areas[area_id] = name
                break

    generated_geojson = getattr(coordinator.data.map, "generated_geojson", None) or {}
    for feature in generated_geojson.get("features", []):
        properties = feature.get("properties", {})
        if properties.get("type_name") != "area":
            continue
        try:
            area_id = int(properties.get("hash"))
        except (TypeError, ValueError):
            continue
        if area_id not in map_area_hashes:
            continue
        name = str(properties.get("Name") or properties.get("title") or "").strip()
        if name and not _is_generic_area_name(name, area_id):
            named_areas[area_id] = name

    for area in coordinator.data.map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        if area_id not in map_area_hashes:
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if name and not _is_generic_area_name(name, area_id):
            named_areas[area_id] = name

    return named_areas


def _fallback_named_areas(
    coordinator: MammotionReportUpdateCoordinator, current_areas: dict[int, str]
) -> dict[int, str]:
    """Return named areas that are known but not present in the current map payload."""
    current_names = {name.lower() for name in current_areas.values()}
    named_areas: dict[int, str] = {}

    for area in coordinator.data.map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if not name or _is_generic_area_name(name, area_id):
            continue
        if name.lower() in current_names:
            continue
        named_areas[area_id] = name
        current_names.add(name.lower())

    return named_areas


def _known_area_hashes(coordinator: MammotionBaseUpdateCoordinator) -> set[int]:
    """Return area hashes that the mower currently advertises as selectable."""
    mower_map = getattr(coordinator.data, "map", None)
    if mower_map is None:
        return set()

    area_hashes: set[int] = {
        int(k) for k in mower_map.area.keys() if str(k).lstrip("-").isdigit()
    }
    for area in mower_map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if name and not _is_generic_area_name(name, area_id):
            area_hashes.add(area_id)

    return area_hashes


def _area_names_loaded(coordinator: MammotionBaseUpdateCoordinator) -> bool:
    """Return True when the mower has supplied its named area list."""
    mower_map = getattr(coordinator.data, "map", None)
    return bool(getattr(mower_map, "area_name", None))


def _is_generic_area_name(name: str, area_id: int) -> bool:
    """Return True for placeholder area names reported by Mammotion firmware."""
    normalized = name.strip().lower()
    return (
        normalized in {"path", f"area {area_id}", str(area_id)}
        or bool(_PYMAMMOTION_AUTO_NAME.match(normalized))
    )


def _async_clean_stale_area_registry_entries(
    coordinator: MammotionReportUpdateCoordinator,
    all_current_areas: set[int],
    area_names_by_hash: dict[int, str],
) -> None:
    """Remove area entity registry entries whose hashes are no longer on the device.

    Older named area entity unique IDs used the mutable area name, so both current
    hash entries and old name entries are cleaned.
    """
    registry = er.async_get(coordinator.hass)
    prefix = f"{coordinator.unique_name}_"
    active_unique_ids = {
        f"{coordinator.unique_name}_{_area_entity_key(area_id, name)}"
        for area_id, name in area_names_by_hash.items()
    }
    for entry in list(registry.entities.values()):
        if entry.domain != SWITCH_DOMAIN or entry.platform != DOMAIN:
            continue
        if not entry.unique_id.startswith(prefix):
            continue
        if entry.unique_id in active_unique_ids:
            continue
        suffix = entry.unique_id[len(prefix) :]
        if entry.translation_key != "area" and not suffix.startswith("area_"):
            continue
        if suffix.lstrip("-").isdigit() and int(suffix) not in all_current_areas:
            registry.async_remove(entry.entity_id)
            continue
        if suffix.startswith("area_"):
            registry.async_remove(entry.entity_id)


def _async_migrate_named_area_registry_entries(
    coordinator: MammotionReportUpdateCoordinator,
    area_names_by_hash: dict[int, str],
) -> None:
    """Preserve registry metadata while replacing legacy name-based IDs."""
    registry = er.async_get(coordinator.hass)
    for area_id, name in area_names_by_hash.items():
        if _is_generic_area_name(name, area_id):
            continue
        legacy_unique_id = f"{coordinator.unique_name}_area_{slugify(name)}"
        legacy_entity_id = registry.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, legacy_unique_id
        )
        if legacy_entity_id is None:
            continue
        new_unique_id = f"{coordinator.unique_name}_{area_id}"
        if conflicting_entity_id := registry.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, new_unique_id
        ):
            if conflicting_entity_id != legacy_entity_id:
                legacy_entry = registry.entities[legacy_entity_id]
                conflicting_entry = registry.entities[conflicting_entity_id]
                if _registry_customization_score(legacy_entry) <= (
                    _registry_customization_score(conflicting_entry)
                ):
                    registry.async_remove(legacy_entity_id)
                    continue
                registry.async_remove(conflicting_entity_id)
        registry.async_update_entity(
            legacy_entity_id, new_unique_id=new_unique_id
        )


def _registry_customization_score(entry: er.RegistryEntry) -> int:
    """Count explicit HA customizations used to choose a migration winner."""
    return sum(
        bool(getattr(entry, field, None))
        for field in (
            "area_id",
            "disabled_by",
            "hidden_by",
            "icon",
            "labels",
            "name",
        )
    )


def _cleanup_removed_yuka_2_switches(
    hass: HomeAssistant,
    device_name: str,
) -> None:
    """Remove switch entities that are not exposed by the Yuka app controls."""
    if not is_yuka_2(device_name):
        return
    registry = er.async_get(hass)
    for key in (
        "manual_light",
        "night_light",
        "is_mow",
        "is_dump",
        "is_edge",
        "rain_tactics",
    ):
        entity_id = registry.async_get_entity_id(
            SWITCH_DOMAIN,
            DOMAIN,
            f"{device_name}_{key}",
        )
        if entity_id:
            registry.async_remove(entity_id)


def _cleanup_cloud_switch_without_account(
    hass: HomeAssistant,
    unique_name: str,
    has_cloud_account: bool,
) -> None:
    """Remove a stale cloud switch after cloud credentials are removed."""
    if has_cloud_account:
        return
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        SWITCH_DOMAIN,
        DOMAIN,
        f"{unique_name}_cloud_enabled",
    )
    if entity_id:
        registry.async_remove(entity_id)


def async_remove_stale_area_entities(
    coordinator: MammotionBaseUpdateCoordinator,
    old_areas: set[int],
) -> None:
    """Remove area switch sensors from Home Assistant."""
    registry = er.async_get(coordinator.hass)

    for area in old_areas:
        entity_id = registry.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, f"{coordinator.unique_name}_{area}"
        )
        if entity_id:
            registry.async_remove(entity_id)


class MammotionSpinoSwitchEntity(MammotionBaseSpinoEntity, SwitchEntity):
    """Representation of a Mammotion Spino pool cleaner switch entity."""

    entity_description: MammotionSpinoSwitchEntityDescription

    def __init__(
        self,
        coordinator: MammotionSpinoCoordinator,
        entity_description: MammotionSpinoSwitchEntityDescription,
    ) -> None:
        """Initialize the Spino switch entity."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key

    @property
    def is_on(self) -> bool:
        """Return True if the toggle is on."""
        return self.entity_description.is_on_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the toggle on.

        No explicit refresh — the device echoes the new value in a
        ``bidire_comm_cmd`` response, which the reducer applies and the
        coordinator's ``_on_state_changed`` callback pushes to this entity.
        """
        await self.entity_description.set_fn(self.coordinator, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the toggle off (state updates via the device's response event)."""
        await self.entity_description.set_fn(self.coordinator, False)
