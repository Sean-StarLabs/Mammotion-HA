# Yuka control validation

This matrix validates mower controls against device-reported telemetry. A command
acknowledgement is not evidence that the mower changed state.

## Safety rules

- Hold one command transaction per mower at a time.
- Refresh report telemetry before deciding which command sequence to send.
- Capture the report generation before sending and require newer telemetry.
- Stop or dock the mower after every route test.
- User controls take priority over map and route fetches.
- Before a start command is dispatched, pause, stop, or dock cancels route
  planning. After dispatch, the start remains tracked until fresh telemetry
  arrives, then the queued safety action runs against the reported state.
- Do not synthesize trails from sampled GPS positions.

## Command matrix

| Initial state | User action | Wire sequence | Required reported result |
| --- | --- | --- | --- |
| Docked or ready | Start current task | Start | Working |
| Docked or ready | Start explicit areas | Plan route, start | Working on requested route |
| Paused with breakpoint | Start | Query route, resume | Working |
| Returning | Start | Cancel return, query route if needed, resume or start | Working |
| Working | Pause | Pause | Paused |
| Working | Stop | Pause, cancel job | Ready |
| Paused | Stop | Cancel job | Ready |
| Returning | Stop | Cancel return, cancel job if a breakpoint remains | Ready with no resumable task |
| Working | Dock | Pause, return to dock | Returning, then docked |
| Paused or ready away from dock | Dock | Return to dock | Returning, then docked |
| Returning | Dock | No command | Returning |
| Docked | Dock | No command | Docked |

Start allows 90 seconds and resume allows 60 seconds for reported `Working`;
pause allows 20 seconds; stop and return-to-dock transitions allow 30 seconds.
A matching task acknowledgement with a non-zero result fails immediately. A
missing acknowledgement may still succeed only when fresh telemetry reports the
required state.

## Route scenarios

1. Back garden: start, pause, resume, stop, start again, then dock.
2. Front garden: start and allow the route attempt to begin, then stop and dock.
3. Send overlapping user actions and verify they execute serially.
4. Repeat after an HA restart to verify no acknowledgement or restored cache is
   mistaken for live mower state.
5. Repeat with BLE unavailable to exercise cloud timing and with BLE available
   to exercise local transport.

## Live results: YUKA_ML 2.3.30.26

Tested on 2026-08-30 with BLE connected:

| Scenario | Observed result |
| --- | --- |
| Back garden start | Working in 0.8 to 16.3 seconds across warm and post-restart runs |
| Pause | Paused in 1.1 seconds |
| Resume | Working in 2.1 seconds |
| Stop | Pause and cancel completed in 5.9 seconds |
| Return to dock | Returning in 1.1 seconds; physically docked in about 60 seconds |
| Front garden start | Working in 43.6 seconds; stopped and recalled before reaching the area |
| Restart while working | Fresh state recovered; pause, cancel, and dock remained usable |
| Start preempted before dispatch | Start and dock both returned successfully; mower remained docked for 105 seconds |
| Start followed by dock | Working observed after 11 seconds; returning after dock request; physically docked after 42 seconds |

## Native route display

`YUKA_ML` firmware `2.3.30.26` does not acknowledge the type-18 dynamics-line
request. Its `cover_path_upload` response for the accepted 64-bit route hash is
`result=1`, `total_frame=0`, with no packets. A single legacy `toapp_zigzag`
probe for that hash also returned no frame. Do not poll any of these protocols
repeatedly on this model.

Persisting or displaying sampled mower GPS positions as a trail is out of scope.
The Yuka therefore displays no route line unless a future firmware or protocol
capture provides native geometry.
