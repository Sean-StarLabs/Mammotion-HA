# Yuka control validation

This matrix validates mower controls against device-reported telemetry. A
command acknowledgement alone is not evidence that the mower changed state.

## Safety rules

- Serialize commands per mower.
- Refresh report telemetry before choosing a command sequence.
- Capture the report generation before sending and require newer telemetry.
- Let safety actions preempt route reads and planning.
- Stop or dock the mower after every route test.
- Do not synthesize trails from sampled GPS positions.

## Command matrix

| Initial state | User action | Wire sequence | Required reported result |
| --- | --- | --- | --- |
| Docked or ready | Start current task | Start | Working |
| Docked or ready | Start explicit areas | Plan route, start | Working on requested route |
| Paused with breakpoint | Start | Query route, resume | Working |
| Returning | Start | Cancel return, query route if needed, resume or start | Working |
| Working | Pause | Pause | Paused |
| Returning | Pause | Cancel return | Paused or ready |
| Working | Stop | Pause, cancel job | Ready with no breakpoint |
| Paused | Stop | Cancel job | Ready with no breakpoint |
| Returning | Stop | Cancel return, cancel job if a breakpoint remains | Ready with no breakpoint |
| Working | Dock | Pause, return to dock | Returning, then docked |
| Paused or ready away from dock | Dock | Return to dock | Returning, then docked |
| Returning | Dock | No command | Returning |
| Docked | Dock | No command | Docked |

Fresh starts allow 240 seconds and resumes allow 60 seconds for reported
`Working`; pause allows 20 seconds; stop and return transitions allow 30
seconds. The longer fresh-start window covers a live Yuka route that took about
200 seconds to leave its charger after accepting the route. A matching task
acknowledgement with a non-zero result fails immediately. A missing
acknowledgement may still succeed only when newer report telemetry reports the
required state.

When a safety action preempts a Start that may already have reached the mower,
Pause uses the fresh-start confirmation window so Dock can wait through route
planning. If the mower reports a paused breakpoint while still on its charger,
Dock cancels that retained task before returning.

## Runtime scenarios

1. Back garden: start, pause, resume, stop, start again, then dock.
2. Front garden: start and allow the route attempt to begin, then stop and dock.
3. Start a route and issue a safety action while planning is still pending.
4. Repeat after a Home Assistant restart to ensure restored state is not treated
   as fresh telemetry.
5. Repeat with BLE unavailable and available to cover both command transports.

## Live results

The clean stack was exercised on a Yuka on 1-2 September 2026:

- Back Garden started, paused, resumed, stopped, restarted, and docked.
- Front Garden accepted a route attempt before Stop and Dock returned the mower.
- Dock preempted an accepted Start during route planning. The mower reported the
  paused breakpoint after 203.7 seconds; Dock cleared it and finished docked.
- Back Garden selection, Front Garden deselection, working speed, and obstacle
  detection survived Home Assistant restarts.
- Docked working-speed and obstacle-detection changes applied and were restored
  to their original values.
- Commands completed over BLE while cloud authentication was unavailable.

Active-job route-setting changes were not repeated during the overnight final
pass and are not claimed as functionally validated here.
