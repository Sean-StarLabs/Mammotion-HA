# Entity migration

Mower task state is exposed by the primary `lawn_mower` entity. The integration
removes the following duplicate sensor registry entries during setup:

| Retired sensor suffix | Replacement |
| --- | --- |
| `activity_mode` | State of `lawn_mower.<mower>` |
| `progress` | `cleaning_progress` attribute |
| `work_area` | `current_area` attribute |
| `total_time` | `total_time_minutes` attribute |
| `elapsed_time` | `elapsed_time_minutes` attribute |
| `left_time` | `remaining_time_minutes` attribute |
| `<area>_task_area` | Entry in the hash-keyed `task_areas` attribute |

Update dashboards, templates, and automations that reference a retired entity.
Battery and independent diagnostic sensors remain separate entities.
