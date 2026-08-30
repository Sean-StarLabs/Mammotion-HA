"""Mammotion map image entities."""

from __future__ import annotations

import datetime
import json
import time
from copy import copy
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback

from . import MammotionConfigEntry
from .const import (
    CONF_MAP_BASE_LAYER,
    MAP_BASE_LAYER_OPENSTREETMAP,
    MAP_BASE_LAYER_SATELLITE,
)
from .coordinator import MammotionReportUpdateCoordinator, is_active_mow_task
from .entity import MammotionBaseEntity
from .geojson_utils import apply_coord, apply_geojson_offset
from .map_renderer import (
    ESRI_WORLD_IMAGERY_TILE_PROVIDER,
    OPENSTREETMAP_TILE_PROVIDER,
    MapTileProvider,
    placeholder_png,
    render_map_png,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammotionConfigEntry,
    async_add_entities,
) -> None:
    """Set up map image entities."""
    async_add_entities(
        MammotionMapImage(mower.reporting_coordinator, hass)
        for mower in entry.runtime_data.mowers
    )


class MammotionMapImage(MammotionBaseEntity, ImageEntity):
    """Static rendered mower map."""

    _RENDER_CACHE_SECONDS = 300.0
    _PLACEHOLDER_CONTENT_KEY = "placeholder"

    _attr_translation_key = "map"
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MammotionReportUpdateCoordinator,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the map image."""
        MammotionBaseEntity.__init__(self, coordinator, "map")
        ImageEntity.__init__(self, hass)
        self._attr_image_last_updated = datetime.datetime.now(datetime.UTC)
        self._cached_png: bytes | None = None
        self._last_content_key: str | None = None
        self._notified_content_key: str | None = None
        self._notified_live_key: tuple[Any, ...] | None = None
        self._last_render_time = 0.0

    async def async_added_to_hass(self) -> None:
        """Refresh rendered image when the mower map changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.subscribe_map_updated(self._handle_map_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Invalidate image when live mower telemetry changes."""
        live_key = self._current_live_key()
        if live_key != self._notified_live_key:
            self._notified_live_key = live_key
            self._attr_image_last_updated = datetime.datetime.now(datetime.UTC)
        super()._handle_coordinator_update()

    @callback
    def _handle_map_update(self) -> None:
        """Invalidate image when static map data changes."""
        self._cached_png = None
        self._notified_content_key = None
        self._notified_live_key = None
        self._attr_image_last_updated = datetime.datetime.now(datetime.UTC)
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return a rendered map image."""
        payload = self._image_payload()
        if payload is None:
            self._notified_content_key = self._PLACEHOLDER_CONTENT_KEY
            self._notified_live_key = self._current_live_key()
            return placeholder_png()

        geojson, mower_location, mower_trail, tile_provider, content_key = payload
        now = time.monotonic()
        if (
            self._cached_png is not None
            and content_key == self._last_content_key
            and now - self._last_render_time < self._RENDER_CACHE_SECONDS
        ):
            return self._cached_png

        tile_cache_dir = self.hass.config.path(
            ".storage",
            "mammotion_map_tiles",
            tile_provider.key,
        )
        self._cached_png = await self.hass.async_add_executor_job(
            render_map_png,
            geojson,
            tile_cache_dir,
            mower_location,
            mower_trail,
            tile_provider,
        )
        self._last_content_key = content_key
        self._notified_content_key = content_key
        self._notified_live_key = self._current_live_key()
        self._last_render_time = now
        return self._cached_png

    def _current_live_key(self) -> tuple[Any, ...]:
        """Return a cheap key for live/retained trail image updates."""
        mower = self.coordinator.manager.get_device_by_name(
            self.coordinator.device_name
        )
        if mower is None:
            return (self._PLACEHOLDER_CONTENT_KEY,)

        trail = self._native_trail_coordinates(mower)
        last_trail_point = trail[-1] if trail else None
        active = is_active_mow_task(mower)
        offset_key = (
            round(float(self.coordinator.map_offset_lat), 7),
            round(float(self.coordinator.map_offset_lon), 7),
        )
        mower_location = self._offset_location(mower.location.device)
        if not active and not trail:
            return (
                "idle",
                self._tile_provider().key,
                offset_key,
                self._rounded_location(mower_location),
            )

        return (
            "live" if active else "retained",
            self._tile_provider().key,
            offset_key,
            len(trail),
            self._rounded_point(last_trail_point),
            self._rounded_location(mower_location),
        )

    def _image_payload(
        self,
    ) -> (
        tuple[
            dict[str, Any] | None,
            Any | None,
            list[tuple[float, float]],
            MapTileProvider,
            str,
        ]
        | None
    ):
        """Return render inputs and their stable content key."""
        mower = self.coordinator.manager.get_device_by_name(
            self.coordinator.device_name
        )
        if mower is None:
            return None

        geojson = self._merged_geojson(mower)
        offset_lat = self.coordinator.map_offset_lat
        offset_lon = self.coordinator.map_offset_lon
        if geojson is not None:
            geojson = apply_geojson_offset(geojson, offset_lat, offset_lon)
        mower_location = self._offset_location(mower.location.device)
        mower_trail: list[tuple[float, float]] = []
        tile_provider = self._tile_provider()
        content_key = self._content_key(
            geojson,
            mower_location,
            mower_trail,
            tile_provider.key,
        )
        return geojson, mower_location, mower_trail, tile_provider, content_key

    def _tile_provider(self) -> MapTileProvider:
        """Return the configured map background provider."""
        base_layer = self.coordinator.config_entry.options.get(
            CONF_MAP_BASE_LAYER,
            MAP_BASE_LAYER_OPENSTREETMAP,
        )
        if base_layer == MAP_BASE_LAYER_SATELLITE:
            return ESRI_WORLD_IMAGERY_TILE_PROVIDER
        return OPENSTREETMAP_TILE_PROVIDER

    def _merged_geojson(self, mower: Any) -> dict[str, Any] | None:
        base_geojson = MammotionMapImage._base_geojson(
            getattr(mower.map, "generated_geojson", None)
        )
        feature_collections = [base_geojson]
        active = is_active_mow_task(mower)
        if active and MammotionMapImage._has_current_native_route(mower):
            feature_collections.append(
                MammotionMapImage._line_geojson(
                    getattr(mower.map, "generated_mow_progress_geojson", None),
                )
            )
        if active:
            feature_collections.append(
                MammotionMapImage._line_geojson(
                    getattr(mower.map, "generated_dynamics_line_geojson", None),
                )
            )
        features: list[dict[str, Any]] = []
        for geojson in feature_collections:
            if isinstance(geojson, dict):
                features.extend(geojson.get("features") or [])
        if not features:
            return None
        return {
            "type": "FeatureCollection",
            "name": "Mammotion Map",
            "features": features,
        }

    @staticmethod
    def _native_trail_coordinates(mower: Any) -> list[tuple[float, float]]:
        """Return device-provided dynamics-line coordinates for cache keys."""
        geojson = getattr(mower.map, "generated_dynamics_line_geojson", None)
        if not isinstance(geojson, dict):
            return []
        for feature in geojson.get("features") or []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            if properties.get("type_name") != "dynamics_line":
                continue
            if geometry.get("type") != "LineString":
                continue
            coordinates = geometry.get("coordinates") or []
            return [
                (float(point[0]), float(point[1]))
                for point in coordinates
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
        return []

    @staticmethod
    def _has_current_native_route(mower: Any) -> bool:
        """Return whether cached path geometry matches confirmed live telemetry."""
        try:
            work = mower.report_data.work
            path_hash = int(work.path_hash)
            ub_path_hash = int(work.ub_path_hash)
            effective_hash = (
                path_hash
                if path_hash not in (0, 1)
                else ub_path_hash if ub_path_hash not in (0, 1) else 0
            )
            return effective_hash != 0 and mower.map.has_mow_path_for_hash(
                effective_hash
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _rounded_point(point: tuple[float, float] | None) -> tuple[float, float] | None:
        """Round lon/lat samples enough to avoid timestamp churn from float noise."""
        if point is None:
            return None
        return (round(float(point[0]), 7), round(float(point[1]), 7))

    @staticmethod
    def _rounded_location(location: Any | None) -> tuple[float, float] | None:
        """Return a rounded lon/lat tuple from a pymammotion location object."""
        if location is None:
            return None
        try:
            return (
                round(float(location.longitude), 7),
                round(float(location.latitude), 7),
            )
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _base_geojson(geojson: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep persistent map geometry and drop stale route/progress overlays."""
        if not isinstance(geojson, dict):
            return None
        features = [
            feature
            for feature in geojson.get("features") or []
            if MammotionMapImage._is_base_map_feature(feature)
        ]
        if not features:
            return None
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    def _line_geojson(
        geojson: dict[str, Any] | None, *, skip_trail_features: bool = False
    ) -> dict[str, Any] | None:
        """Keep only line geometry from live task overlays."""
        if not isinstance(geojson, dict):
            return None
        features = [
            feature
            for feature in geojson.get("features") or []
            if (feature.get("geometry") or {}).get("type")
            in {"LineString", "MultiLineString"}
            and not (
                skip_trail_features and MammotionMapImage._is_trail_feature(feature)
            )
        ]
        if not features:
            return None
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    def _is_base_map_feature(feature: dict[str, Any]) -> bool:
        properties = feature.get("properties") or {}
        type_name = str(
            properties.get("type_name")
            or properties.get("type")
            or properties.get("Type")
            or ""
        ).lower()
        return type_name in {
            "area",
            "charging_station",
            "dump",
            "no_go_zone",
            "obstacle",
            "station",
            "virtual_wall",
            "visual_obstacle_zone",
            "visual_safety_zone",
        }

    @staticmethod
    def _is_trail_feature(feature: dict[str, Any]) -> bool:
        properties = feature.get("properties") or {}
        type_name = str(
            properties.get("type_name")
            or properties.get("type")
            or properties.get("Type")
            or ""
        ).lower()
        return type_name == "trail"

    def _offset_location(self, mower_location: Any) -> Any:
        if mower_location is None:
            return None
        latitude = getattr(mower_location, "latitude", None)
        longitude = getattr(mower_location, "longitude", None)
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return None
        if latitude == 0.0 and longitude == 0.0:
            return None
        shifted = apply_coord(
            [longitude, latitude],
            latitude,
            self.coordinator.map_offset_lat,
            self.coordinator.map_offset_lon,
        )
        shifted_location = copy(mower_location)
        shifted_location.longitude = shifted[0]
        shifted_location.latitude = shifted[1]
        return shifted_location

    def _offset_trail(
        self, mower_trail: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        shifted_trail: list[tuple[float, float]] = []
        for lon, lat in mower_trail:
            try:
                longitude = float(lon)
                latitude = float(lat)
            except (TypeError, ValueError):
                continue
            shifted = apply_coord(
                [longitude, latitude],
                latitude,
                self.coordinator.map_offset_lat,
                self.coordinator.map_offset_lon,
            )
            shifted_trail.append((shifted[0], shifted[1]))
        return shifted_trail

    @staticmethod
    def _content_key(
        geojson: dict[str, Any] | None,
        mower_location: Any | None,
        mower_trail: list[tuple[float, float]],
        tile_provider_key: str,
    ) -> str:
        location_key = None
        if mower_location is not None:
            location_key = (
                round(float(getattr(mower_location, "latitude", 0.0) or 0.0), 7),
                round(float(getattr(mower_location, "longitude", 0.0) or 0.0), 7),
            )
        payload = {
            "geojson": geojson,
            "location": location_key,
            "trail": [
                (round(float(lon), 7), round(float(lat), 7))
                for lon, lat in mower_trail[-80:]
            ],
            "tile_provider": tile_provider_key,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
