"""Pure helpers for stable mowing-area identity."""

import re
from typing import Any

AUTO_AREA_NAME = re.compile(r"^area\s+\d+$", re.IGNORECASE)


def merged_registry_customizations(primary: Any, secondary: Any) -> dict[str, Any]:
    """Merge non-conflicting entity customizations into the chosen survivor."""
    aliases = list(primary.aliases)
    aliases.extend(alias for alias in secondary.aliases if alias not in aliases)
    return {
        "aliases": aliases,
        "area_id": primary.area_id or secondary.area_id,
        "categories": {**secondary.categories, **primary.categories},
        "disabled_by": primary.disabled_by or secondary.disabled_by,
        "hidden_by": primary.hidden_by or secondary.hidden_by,
        "icon": primary.icon or secondary.icon,
        "labels": set(primary.labels) | set(secondary.labels),
        "name": primary.name or secondary.name,
    }


def merge_registry_entry_collision(
    registry: Any,
    survivor_entity_id: str,
    duplicate_entity_id: str,
) -> None:
    """Merge registry metadata into the survivor before removing a duplicate."""
    survivor = registry.entities[survivor_entity_id]
    duplicate = registry.entities[duplicate_entity_id]
    registry.async_update_entity(
        survivor_entity_id,
        **merged_registry_customizations(survivor, duplicate),
    )
    registry.async_remove(duplicate_entity_id)


def area_entity_key(area_id: int, _name: str) -> str:
    """Return the immutable device hash used as an area's entity key."""
    return f"{area_id}"


def display_area_name(area_id: int, name: str) -> str:
    """Return a user-facing name while keeping placeholders out of identity."""
    if not name or is_generic_area_name(name, area_id):
        return f"area {area_id}"
    return name


def active_named_areas(mower_map: Any, map_area_hashes: set[int]) -> dict[int, str]:  # noqa: C901
    """Return real area names keyed by active mower area hash."""
    if not map_area_hashes:
        return {}

    named_areas: dict[int, str] = {}
    for area_hash, area_data in mower_map.area.items():
        try:
            area_id = int(area_hash)
        except (TypeError, ValueError):
            continue
        if area_id not in map_area_hashes:
            continue
        for frame in getattr(area_data, "data", []) or []:
            name_time = getattr(frame, "name_time", None)
            name = str(getattr(name_time, "name", "") or "").strip()
            if name and not is_generic_area_name(name, area_id):
                named_areas[area_id] = name
                break

    generated_geojson = getattr(mower_map, "generated_geojson", None) or {}
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
        if name and not is_generic_area_name(name, area_id):
            named_areas[area_id] = name

    for area in mower_map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        if area_id not in map_area_hashes:
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if name and not is_generic_area_name(name, area_id):
            named_areas[area_id] = name

    return named_areas


def fallback_named_areas(
    mower_map: Any,
    current_areas: dict[int, str],
    active_hashes: set[int] | None = None,
) -> dict[int, str]:
    """Return known named areas absent from the current map payload."""
    current_names = {name.lower() for name in current_areas.values()}
    named_areas: dict[int, str] = {}

    for area in mower_map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        if active_hashes is not None and area_id not in active_hashes:
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if not name or is_generic_area_name(name, area_id):
            continue
        if name.lower() in current_names:
            continue
        named_areas[area_id] = name
        current_names.add(name.lower())

    return named_areas


def known_area_hashes(mower_map: Any) -> set[int]:
    """Return area hashes that the mower advertises as selectable."""
    if (
        manifest_hashes := getattr(mower_map, "area_manifest_hashes", None)
    ) is not None:
        return set(manifest_hashes)
    area_hashes: set[int] = {
        int(key) for key in mower_map.area if str(key).lstrip("-").isdigit()
    }
    for area in mower_map.area_name:
        try:
            area_id = int(area.hash)
        except (TypeError, ValueError):
            continue
        name = str(getattr(area, "name", "") or "").strip()
        if name and not is_generic_area_name(name, area_id):
            area_hashes.add(area_id)
    return area_hashes


def area_names_loaded(mower_map: Any) -> bool:
    """Return whether the mower supplied its named area list."""
    return bool(getattr(mower_map, "area_name", None))


def is_generic_area_name(name: str, area_id: int) -> bool:
    """Return whether a name is a firmware-generated placeholder."""
    normalized = name.strip().lower()
    return (
        normalized in {"path", f"area {area_id}", str(area_id)}
        or bool(AUTO_AREA_NAME.match(normalized))
    )
