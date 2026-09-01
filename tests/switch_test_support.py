"""Load Mammotion switch entities without integration setup side effects."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_switch_module() -> ModuleType:
    """Return an isolated import of the switch platform."""
    package_name = "switch_test_mammotion"
    package = ModuleType(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "mammotion")
    ]
    package.MammotionConfigEntry = object
    sys.modules[package_name] = package

    coordinator = ModuleType(f"{package_name}.coordinator")
    coordinator.MammotionBaseUpdateCoordinator = object
    coordinator.MammotionReportUpdateCoordinator = object
    coordinator.MammotionSpinoCoordinator = object
    sys.modules[coordinator.__name__] = coordinator

    entity = ModuleType(f"{package_name}.entity")
    entity.MammotionBaseEntity = type("MammotionBaseEntity", (), {})
    entity.MammotionBaseSpinoEntity = type("MammotionBaseSpinoEntity", (), {})
    sys.modules[entity.__name__] = entity

    return importlib.import_module(f"{package_name}.switch")
