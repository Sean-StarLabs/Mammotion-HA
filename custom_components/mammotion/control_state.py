"""Pure mower control-state decisions."""

from __future__ import annotations

from dataclasses import dataclass

from pymammotion.utility.constant import WorkMode


@dataclass(frozen=True, slots=True)
class MowerControlState:
    """Reported facts used to expose mower commands."""

    mode: int | None
    command_ready: bool
    on_charger: bool
    breakpoint_info: int
    selected_area_count: int

    @property
    def can_start(self) -> bool:
        """Return whether start means a valid new or resumed task."""
        if not self.command_ready:
            return False
        if self.mode in (WorkMode.MODE_PAUSE, WorkMode.MODE_CHARGING_PAUSE):
            return self.breakpoint_info != 0
        if self.mode in (
            WorkMode.MODE_READY,
            WorkMode.MODE_INITIALIZATION,
            WorkMode.MODE_RETURNING,
        ):
            return self.breakpoint_info != 0 or self.selected_area_count > 0
        return False

    @property
    def can_pause(self) -> bool:
        """Return whether active work or a dock return can be paused."""
        return self.command_ready and self.mode in (
            WorkMode.MODE_WORKING,
            WorkMode.MODE_RETURNING,
        )

    @property
    def can_dock(self) -> bool:
        """Return whether return-to-dock can start."""
        return (
            self.command_ready
            and not self.on_charger
            and self.mode
            in (WorkMode.MODE_WORKING, WorkMode.MODE_PAUSE, WorkMode.MODE_READY)
        )

    @property
    def can_cancel(self) -> bool:
        """Return whether a task or breakpoint can be cancelled."""
        return self.command_ready and (
            self.mode
            in (
                WorkMode.MODE_WORKING,
                WorkMode.MODE_PAUSE,
                WorkMode.MODE_CHARGING_PAUSE,
                WorkMode.MODE_RETURNING,
            )
            or (self.mode == WorkMode.MODE_READY and self.breakpoint_info != 0)
        )
