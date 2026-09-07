"""Tests for mower control availability."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pymammotion.utility.constant import WorkMode

_SPEC = importlib.util.spec_from_file_location(
    "mammotion_control_state",
    Path(__file__).parents[1] / "custom_components/mammotion/control_state.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
MowerControlState = _MODULE.MowerControlState
route_setting_available = _MODULE.route_setting_available


@pytest.mark.parametrize(
    ("mode", "breakpoint", "areas", "expected"),
    [
        (WorkMode.MODE_READY, 0, 0, (False, False, True, False)),
        (WorkMode.MODE_READY, 0, 1, (True, False, True, False)),
        (WorkMode.MODE_READY, 1, 0, (True, False, True, True)),
        (WorkMode.MODE_WORKING, 1, 1, (False, True, True, True)),
        (WorkMode.MODE_PAUSE, 1, 1, (True, False, True, True)),
        (WorkMode.MODE_CHARGING_PAUSE, 1, 1, (True, False, False, True)),
        (WorkMode.MODE_RETURNING, 1, 1, (True, True, False, True)),
        (WorkMode.MODE_LOCK, 1, 1, (False, False, False, False)),
    ],
)
def test_control_matrix(
    mode: int,
    breakpoint: int,
    areas: int,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    """Reported mode, route state, and selection determine every control."""
    state = MowerControlState(
        mode=mode,
        command_ready=True,
        on_charger=mode == WorkMode.MODE_CHARGING_PAUSE,
        breakpoint_info=breakpoint,
        selected_area_count=areas,
    )

    assert (
        state.can_start,
        state.can_pause,
        state.can_dock,
        state.can_cancel,
    ) == expected


def test_commands_are_hidden_without_a_usable_transport() -> None:
    """Cached telemetry cannot expose controls when no transport can send."""
    state = MowerControlState(
        mode=WorkMode.MODE_WORKING,
        command_ready=False,
        on_charger=False,
        breakpoint_info=1,
        selected_area_count=1,
    )

    assert not any(
        (state.can_start, state.can_pause, state.can_dock, state.can_cancel)
    )


def test_dock_is_hidden_when_position_reports_charger() -> None:
    """A ready mower already at its charger does not offer a redundant dock action."""
    state = MowerControlState(
        mode=WorkMode.MODE_READY,
        command_ready=True,
        on_charger=True,
        breakpoint_info=0,
        selected_area_count=1,
    )

    assert state.can_start
    assert not state.can_dock


@pytest.mark.parametrize(
    ("mode", "runtime_supported", "expected"),
    [
        (WorkMode.MODE_READY, False, True),
        (WorkMode.MODE_INITIALIZATION, False, True),
        (WorkMode.MODE_WORKING, True, True),
        (WorkMode.MODE_WORKING, False, False),
        (WorkMode.MODE_PAUSE, True, False),
        (WorkMode.MODE_RETURNING, True, False),
        (WorkMode.MODE_LOCK, True, False),
    ],
)
def test_route_setting_availability(
    mode: int,
    runtime_supported: bool,
    expected: bool,
) -> None:
    """Only verified live modifiers remain available during an active task."""
    assert (
        route_setting_available(mode, runtime_supported=runtime_supported) is expected
    )
