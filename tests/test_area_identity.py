"""Tests for stable Mammotion mowing-area identity."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_helpers() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "mammotion"
        / "area_identity.py"
    )
    spec = importlib.util.spec_from_file_location("mammotion_area_identity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


def _coordinator(
    *,
    area: dict,
    area_name: list,
    area_manifest_hashes: set[int] | None = None,
    generated_geojson: dict | None = None,
):
    mower_map = SimpleNamespace(
        area=area,
        area_name=area_name,
        area_manifest_hashes=area_manifest_hashes,
        generated_geojson=generated_geojson or {},
    )
    return SimpleNamespace(data=SimpleNamespace(map=mower_map))


def test_area_entity_key_uses_device_hash() -> None:
    """Renaming an area does not change its entity registry identity."""
    assert helpers.area_entity_key(123, "Front lawn") == "123"
    assert helpers.area_entity_key(123, "Renamed lawn") == "123"


def test_display_name_replaces_firmware_placeholders() -> None:
    """Missing and generated names remain useful without becoming identity."""
    assert helpers.display_area_name(7, "") == "area 7"
    assert helpers.display_area_name(7, "Path") == "area 7"
    assert helpers.display_area_name(7, "Kitchen lawn") == "Kitchen lawn"
    assert helpers.is_generic_area_name("Area 7", 7)


def test_active_names_prefer_device_name_list() -> None:
    """The explicit device name list overrides names embedded in map geometry."""
    coordinator = _coordinator(
        area={123: SimpleNamespace(data=[])},
        area_name=[SimpleNamespace(hash=123, name="Front lawn")],
        generated_geojson={
            "features": [
                {
                    "properties": {
                        "type_name": "area",
                        "hash": 123,
                        "Name": "Old name",
                    }
                }
            ]
        },
    )

    assert helpers.active_named_areas(coordinator.data.map, {123}) == {
        123: "Front lawn"
    }


def test_fallback_names_do_not_duplicate_current_name() -> None:
    """A stale hash with the same name cannot create a duplicate area entity."""
    coordinator = _coordinator(
        area={456: SimpleNamespace(data=[])},
        area_name=[
            SimpleNamespace(hash=123, name="Front lawn"),
            SimpleNamespace(hash=456, name="Back lawn"),
        ],
    )

    assert helpers.fallback_named_areas(coordinator.data.map, {789: "Front lawn"}) == {
        456: "Back lawn"
    }


def test_deleted_area_name_is_not_selectable() -> None:
    """A retained name cannot resurrect a hash absent from live map geometry."""
    coordinator = _coordinator(
        area={456: SimpleNamespace(data=[])},
        area_name=[
            SimpleNamespace(hash=123, name="Deleted lawn"),
            SimpleNamespace(hash=456, name="Back lawn"),
        ],
        area_manifest_hashes={456},
    )

    manifest = coordinator.data.map.area_manifest_hashes
    assert manifest == {456}
    assert helpers.fallback_named_areas(coordinator.data.map, {}, manifest) == {
        456: "Back lawn"
    }
    assert helpers.known_area_hashes(coordinator.data.map) == {456}


def test_empty_complete_manifest_removes_retained_names() -> None:
    """Deleting the final area leaves an authoritative empty selection."""
    coordinator = _coordinator(
        area={},
        area_name=[SimpleNamespace(hash=123, name="Deleted lawn")],
        area_manifest_hashes=set(),
    )

    manifest = coordinator.data.map.area_manifest_hashes
    assert manifest == set()
    assert helpers.fallback_named_areas(coordinator.data.map, {}, manifest) == {}
    assert helpers.known_area_hashes(coordinator.data.map) == set()


def test_partial_manifest_keeps_transient_name_fallbacks() -> None:
    """An incomplete manifest cannot prune areas while map sync is in flight."""
    coordinator = _coordinator(
        area={456: SimpleNamespace(data=[])},
        area_name=[
            SimpleNamespace(hash=123, name="Front lawn"),
            SimpleNamespace(hash=456, name="Back lawn"),
        ],
        area_manifest_hashes=None,
    )

    assert coordinator.data.map.area_manifest_hashes is None
    assert helpers.known_area_hashes(coordinator.data.map) == {123, 456}


def test_known_hashes_include_named_yuka_areas() -> None:
    """Named areas survive a transient map-area payload gap."""
    coordinator = _coordinator(
        area={},
        area_name=[SimpleNamespace(hash=42, name="Side lawn")],
    )

    assert helpers.known_area_hashes(coordinator.data.map) == {42}


def test_registry_customizations_are_merged_before_collision_cleanup() -> None:
    """A stable-ID collision retains metadata from both registry entries."""
    primary = SimpleNamespace(
        aliases=["rear garden"],
        area_id="garden",
        categories={"scope": "outside"},
        disabled_by=None,
        hidden_by="user",
        icon=None,
        labels={"mower"},
        name="Back lawn",
    )
    secondary = SimpleNamespace(
        aliases=["back lawn"],
        area_id="fallback",
        categories={"season": "summer", "scope": "legacy"},
        disabled_by="user",
        hidden_by=None,
        icon="mdi:grass",
        labels={"outdoors"},
        name="Legacy name",
    )

    assert helpers.merged_registry_customizations(primary, secondary) == {
        "aliases": ["rear garden", "back lawn"],
        "area_id": "garden",
        "categories": {"season": "summer", "scope": "outside"},
        "disabled_by": "user",
        "hidden_by": "user",
        "icon": "mdi:grass",
        "labels": {"mower", "outdoors"},
        "name": "Back lawn",
    }


def test_registry_collision_is_merged_before_duplicate_removal() -> None:
    """A live area rebind cannot delete the duplicate before preserving metadata."""
    survivor = SimpleNamespace(
        aliases=["rear garden"],
        area_id=None,
        categories={},
        disabled_by=None,
        hidden_by=None,
        icon=None,
        labels={"mower"},
        name=None,
    )
    duplicate = SimpleNamespace(
        aliases=["back lawn"],
        area_id="garden",
        categories={"season": "summer"},
        disabled_by="user",
        hidden_by="user",
        icon="mdi:grass",
        labels={"outdoors"},
        name="Back lawn",
    )
    events: list[tuple[str, str]] = []

    class Registry:
        entities = {"switch.old": survivor, "switch.stable": duplicate}

        @staticmethod
        def async_update_entity(entity_id: str, **changes: object) -> None:
            events.append(("update", entity_id))
            assert changes == {
                "aliases": ["rear garden", "back lawn"],
                "area_id": "garden",
                "categories": {"season": "summer"},
                "disabled_by": "user",
                "hidden_by": "user",
                "icon": "mdi:grass",
                "labels": {"mower", "outdoors"},
                "name": "Back lawn",
            }

        @staticmethod
        def async_remove(entity_id: str) -> None:
            events.append(("remove", entity_id))

    helpers.merge_registry_entry_collision(
        Registry(),
        "switch.old",
        "switch.stable",
    )

    assert events == [
        ("update", "switch.old"),
        ("remove", "switch.stable"),
    ]
