"""Focused tests for map image invalidation and native geometry selection."""

# ruff: noqa: SLF001

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock


def _module(name: str, **attributes: object) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_image_module(monkeypatch):
    class ImageEntity:
        def __init__(self, hass):
            self.hass = hass

        def _handle_coordinator_update(self) -> None:
            pass

    class MammotionBaseEntity:
        def __init__(self, coordinator, _key):
            self.coordinator = coordinator

        def _handle_coordinator_update(self) -> None:
            pass

    class DeviceType:
        @staticmethod
        def value_of_str(_device_name):
            return SimpleNamespace(is_support_dynamics_line=lambda _firmware: True)

    package_names = (
        "custom_components",
        "custom_components.mammotion",
        "homeassistant",
        "homeassistant.components",
        "pymammotion",
        "pymammotion.utility",
    )
    for name in package_names:
        package = _module(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.image",
        _module("homeassistant.components.image", ImageEntity=ImageEntity),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.const",
        _module(
            "homeassistant.const",
            EntityCategory=SimpleNamespace(DIAGNOSTIC="diagnostic"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.core",
        _module(
            "homeassistant.core",
            HomeAssistant=object,
            callback=lambda function: function,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pymammotion.utility.constant",
        _module(
            "pymammotion.utility.constant",
            MOWING_ACTIVE_MODES=frozenset({13, 14, 19, 39}),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pymammotion.utility.device_type",
        _module("pymammotion.utility.device_type", DeviceType=DeviceType),
    )
    monkeypatch.setitem(
        sys.modules,
        "pymammotion.utility.map_renderer",
        _module(
            "pymammotion.utility.map_renderer",
            placeholder_png=lambda: b"placeholder",
            render_map_png=Mock(),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.mammotion.coordinator",
        _module(
            "custom_components.mammotion.coordinator",
            MammotionReportUpdateCoordinator=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.mammotion.entity",
        _module(
            "custom_components.mammotion.entity",
            MammotionBaseEntity=MammotionBaseEntity,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.mammotion.geojson_utils",
        _module(
            "custom_components.mammotion.geojson_utils",
            apply_coord=lambda coordinates, *_args: coordinates,
            apply_geojson_offset=lambda geojson, *_args: geojson,
        ),
    )
    sys.modules["custom_components.mammotion"].MammotionConfigEntry = object

    name = "custom_components.mammotion.image_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).parents[1] / "custom_components/mammotion/image.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _line_geojson() -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            }
        ],
    }


def test_map_update_invalidates_render_and_geometry_caches(monkeypatch) -> None:
    """A map callback forces the next image request to rebuild all inputs."""
    module = _load_image_module(monkeypatch)
    entity = module.MammotionMapImage.__new__(module.MammotionMapImage)
    entity._cached_png = b"cached"
    entity._geometry_sources = (object(),)
    entity._notified_content_key = "old"
    entity.async_write_ha_state = Mock()

    entity._handle_map_update()

    assert entity._cached_png is None
    assert entity._geometry_sources is None
    assert entity._notified_content_key is None
    entity.async_write_ha_state.assert_called_once_with()


def test_dynamics_geometry_falls_back_to_native_progress(monkeypatch) -> None:
    """An unusable dynamics payload cannot hide valid device progress lines."""
    module = _load_image_module(monkeypatch)
    progress = _line_geojson()
    dynamics = {
        "type": "FeatureCollection",
        "features": [{"geometry": {"type": "Point", "coordinates": [0, 0]}}],
    }
    mower = SimpleNamespace(
        map=SimpleNamespace(
            generated_geojson=None,
            generated_mow_path_geojson=None,
            generated_dynamics_line_geojson=dynamics,
            generated_mow_progress_geojson=progress,
        ),
        device_firmwares=SimpleNamespace(main_controller="1.0"),
    )
    entity = module.MammotionMapImage.__new__(module.MammotionMapImage)
    entity.coordinator = SimpleNamespace(device_name="Yuka-MLYQ73XB")

    assert entity._geojson_sources(mower)[2] is progress

    mower.map.generated_dynamics_line_geojson = _line_geojson()
    assert (
        entity._geojson_sources(mower)[2] is mower.map.generated_dynamics_line_geojson
    )


def test_active_dynamics_device_keeps_last_complete_map_renderable(monkeypatch) -> None:
    """Live obstacle hash churn must not replace a valid map with a placeholder."""
    module = _load_image_module(monkeypatch)
    mower = SimpleNamespace(
        report_data=SimpleNamespace(
            dev=SimpleNamespace(sys_status=13),
            locations=[SimpleNamespace(bol_hash=200)],
        ),
        device_firmwares=SimpleNamespace(main_controller="1.0"),
        map=SimpleNamespace(
            generated_geojson={"features": [{"type": "Feature"}]},
            is_map_synced=lambda _bol_hash: False,
        ),
    )
    entity = module.MammotionMapImage.__new__(module.MammotionMapImage)
    entity.coordinator = SimpleNamespace(
        device_name="Yuka-MLYQ73XB",
        map_sync_status="out_of_sync",
    )

    assert entity._has_renderable_map(mower)


def test_content_key_tracks_geometry_and_mower_position(monkeypatch) -> None:
    """Geometry replacement and meaningful mower movement invalidate the image."""
    module = _load_image_module(monkeypatch)
    location = SimpleNamespace(longitude=-0.5032998, latitude=51.1379717)
    baseline = module.MammotionMapImage._content_key("geometry-a", location)

    jittered = SimpleNamespace(longitude=-0.5032997, latitude=51.1379718)
    moved = SimpleNamespace(longitude=-0.5032898, latitude=51.1379717)
    assert module.MammotionMapImage._content_key("geometry-a", jittered) == baseline
    assert module.MammotionMapImage._content_key("geometry-a", moved) != baseline
    assert module.MammotionMapImage._content_key("geometry-b", location) != baseline
