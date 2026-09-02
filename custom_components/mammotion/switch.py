"""Support for Mammotion switches."""

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
from pymammotion.data.model.device import PoolCleanerDevice
from pymammotion.data.model.pool_state import SpinoToggle
from pymammotion.utility.device_type import DeviceType

from . import MammotionConfigEntry
from .area_identity import (
    AUTO_AREA_NAME,
    active_named_areas,
    area_entity_key,
    area_names_loaded,
    display_area_name,
    fallback_named_areas,
    is_generic_area_name,
    known_area_hashes,
    merge_registry_entry_collision,
)
from .const import DOMAIN
from .coordinator import (
    MammotionBaseUpdateCoordinator,
    MammotionReportUpdateCoordinator,
    MammotionSpinoCoordinator,
)
from .entity import MammotionBaseEntity, MammotionBaseSpinoEntity
from .operation_settings import retain_known_areas


@dataclass(frozen=True, kw_only=True)
class MammotionSwitchEntityDescription(SwitchEntityDescription):
    """Describes Mammotion switch entity."""

    key: str


@dataclass(frozen=True, kw_only=True)
class MammotionAsyncSwitchEntityDescription(MammotionSwitchEntityDescription):
    """Describes Mammotion switch entity."""

    is_on_func: Callable[[MammotionBaseUpdateCoordinator], bool] | None = None
    set_fn: Callable[[MammotionBaseUpdateCoordinator, bool], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class MammotionConfigSwitchEntityDescription(MammotionSwitchEntityDescription):
    """Describes Mammotion Config switch entity."""

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
        entity_category=EntityCategory.CONFIG,
    ),
    MammotionAsyncSwitchEntityDescription(
        key="cloud_enabled",
        is_on_func=lambda coordinator: coordinator.cloud_enabled,
        set_fn=lambda coordinator, value: coordinator.async_set_cloud_enabled(value),
        entity_category=EntityCategory.CONFIG,
    ),
)

CONFIG_SWITCH_ENTITIES: tuple[MammotionConfigSwitchEntityDescription, ...] = (
    MammotionConfigSwitchEntityDescription(
        key="rain_tactics",
        set_fn=lambda coordinator, value: setattr(
            coordinator.operation_settings, "rain_tactics", int(value)
        ),
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

        if DeviceType.is_luba_pro(device_name):
            entities.extend(
                MammotionSwitchEntity(coordinator, d) for d in AUDIO_SWITCH_ENTITIES
            )

        entities.extend(
            MammotionConfigSwitchEntity(coordinator, d) for d in CONFIG_SWITCH_ENTITIES
        )
        entities.extend(
            MammotionUpdateSwitchEntity(coordinator, d) for d in UPDATE_SWITCH_ENTITIES
        )
        entities.extend(
            MammotionSwitchEntity(coordinator, d) for d in CONNECTIVITY_SWITCH_ENTITIES
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
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON


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
        self.coordinator.async_save_operation_settings()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        self.entity_description.set_fn(self.coordinator, False)
        self.coordinator.async_save_operation_settings()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if self.coordinator.operation_settings_restored:
            return
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON
        self.entity_description.set_fn(self.coordinator, self._attr_is_on)
        self.coordinator.async_save_operation_settings()

    async def async_update(self) -> None:
        """Update the entity state."""


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
                    merge_registry_entry_collision(
                        registry,
                        self.entity_id,
                        conflicting_entity_id,
                    )
            registry.async_update_entity(self.entity_id, new_unique_id=new_unique_id)
        self._attr_unique_id = new_unique_id
        self.area = new_area_id
        self._attr_extra_state_attributes = {"hash": new_area_id}
        if old_area in self.coordinator.operation_settings.areas:
            self.coordinator.operation_settings.areas.remove(old_area)
            if new_area_id not in self.coordinator.operation_settings.areas:
                self.coordinator.operation_settings.areas.append(new_area_id)
            self.coordinator.async_save_operation_settings()
        self._attr_is_on = new_area_id in self.coordinator.operation_settings.areas
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        self._attr_is_on = True
        self.entity_description.set_fn(self.coordinator, True, self.area)
        self.coordinator.async_save_operation_settings()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        self.entity_description.set_fn(self.coordinator, False, self.area)
        self.coordinator.async_save_operation_settings()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        await super().async_added_to_hass()
        # Seed with any existing name override so we only push live user edits.
        self._pushed_name = self.registry_entry.name if self.registry_entry else None
        if self.coordinator.operation_settings_restored:
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
        mower_map = self.coordinator.data.map
        known_hashes = known_area_hashes(mower_map)
        area_membership_loaded = (
            getattr(mower_map, "area_manifest_hashes", None) is not None
        )
        if self.area not in known_hashes and area_membership_loaded:
            await self.async_remove()
            return
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True


@callback
def async_add_area_entities(  # noqa: C901
    coordinator: MammotionReportUpdateCoordinator,
    added_areas: set[int],
    area_entities_by_hash: dict[int, MammotionConfigAreaSwitchEntity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Handle addition of mowing areas."""
    if coordinator.data is None:
        return

    computed = coordinator.data.map.computed_areas or []
    manifest_area_hashes = getattr(
        coordinator.data.map, "area_manifest_hashes", None
    )
    map_area_hashes: set[int] = (
        manifest_area_hashes
        if manifest_area_hashes is not None
        else {
            area_id
            for key in coordinator.data.map.area
            if str(key).lstrip("-").isdigit() and (area_id := int(key)) != 0
        }
    )
    area_membership_loaded = manifest_area_hashes is not None
    area_names_by_hash = active_named_areas(coordinator.data.map, map_area_hashes)
    real_named_map_area_hashes = set(area_names_by_hash)
    for area in computed:
        if manifest_area_hashes is not None and area.hash not in map_area_hashes:
            continue
        raw_area_name = str(getattr(area, "name", "") or "").strip()
        area_name = display_area_name(area.hash, raw_area_name)
        if (
            area.hash in area_names_by_hash
            and is_generic_area_name(area_name, area.hash)
        ):
            continue
        area_names_by_hash[area.hash] = area_name
        if raw_area_name and not is_generic_area_name(raw_area_name, area.hash):
            real_named_map_area_hashes.add(area.hash)
    area_names_by_hash.update(
        fallback_named_areas(
            coordinator.data.map, area_names_by_hash, manifest_area_hashes
        )
    )
    for area_id in sorted(map_area_hashes):
        area_names_by_hash.setdefault(area_id, display_area_name(area_id, ""))
    all_current_areas = set(area_names_by_hash)

    # Trigger re-fetch when the device hasn't yet sent names for all areas.
    # Check before considering generated fallbacks, which are selectable but do
    # not prove that the device's name list has arrived.
    if not DeviceType.is_luba1(coordinator.device_name):
        if map_area_hashes - real_named_map_area_hashes:
            coordinator.hass.async_create_task(coordinator.async_get_area_list())

    # Startup registry cleanup: remove stale entries from previous sessions.
    names_loaded = area_names_loaded(coordinator.data.map)
    area_identity_loaded = names_loaded or DeviceType.is_luba1(
        coordinator.device_name
    )
    if area_membership_loaded and area_identity_loaded:
        _async_migrate_named_area_registry_entries(coordinator, area_names_by_hash)
        if retain_known_areas(coordinator.operation_settings, all_current_areas):
            coordinator.async_save_operation_settings()

    # Early exit when neither the set of area hashes nor any name has changed.
    if all_current_areas == added_areas:
        if all(
            area_id in area_entities_by_hash
            and area_entities_by_hash[area_id].entity_description.name == name
            for area_id, name in area_names_by_hash.items()
        ):
            if area_membership_loaded and area_identity_loaded:
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
                    is_new_auto = bool(AUTO_AREA_NAME.match(new_name))
                    is_cur_auto = bool(AUTO_AREA_NAME.match(current_name))
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
        if not AUTO_AREA_NAME.match(new_name) and existing is not None:
            old_hash = existing.area
            added_areas.discard(existing.area)
            existing.update_area(area_id)
            area_entities_by_hash.pop(old_hash, None)
            area_entities_by_hash[area_id] = existing
            added_areas.add(area_id)
            continue

        # Missing area — add a new entity with the name supplied by computed_areas.
        base_area_switch_entity = MammotionConfigAreaSwitchEntityDescription(
            key=area_entity_key(area_id, new_name),
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

    if area_membership_loaded and area_identity_loaded:
        _async_clean_stale_area_registry_entries(
            coordinator, all_current_areas, area_names_by_hash
        )

    # Remove only after receiving geometry or a complete device manifest. An empty
    # manifest is authoritative; no manifest at all is a transient startup state.
    if area_membership_loaded and area_identity_loaded:
        old_areas = added_areas - all_current_areas
        if old_areas:
            async_remove_stale_area_entities(coordinator, old_areas)
            for area in old_areas:
                added_areas.discard(area)
                area_entities_by_hash.pop(area, None)

    if switch_entities:
        async_add_entities(switch_entities)


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
        f"{coordinator.unique_name}_{area_entity_key(area_id, name)}"
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
        if is_generic_area_name(name, area_id):
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
                if _registry_customization_score(legacy_entry) >= (
                    _registry_customization_score(conflicting_entry)
                ):
                    survivor = legacy_entry
                    duplicate = conflicting_entry
                else:
                    survivor = conflicting_entry
                    duplicate = legacy_entry
                merge_registry_entry_collision(
                    registry,
                    survivor.entity_id,
                    duplicate.entity_id,
                )
                if survivor.entity_id == conflicting_entity_id:
                    continue
        registry.async_update_entity(legacy_entity_id, new_unique_id=new_unique_id)


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
