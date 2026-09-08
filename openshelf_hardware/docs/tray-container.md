# Multi-item trays

A bottomed tray is the container. `storage.bins` config, database bin identities,
item membership and fixed `home_location` remain the existing storage contract.
Replace the separately carried bin with a compatible bottomed tray at that home.
Ordinary single-item transfers continue to use the original IO sequence.
Compartment allocation/numbering is not part of this mechanical prototype.

## Operator/API flow

1. `POST /api/storage/bin-workflows` with `operation`, `upca`, and (for explicit
   multi-item storage) `storage_mode: "bin"` reserves the robot and home cell.
2. `retrieving_bin` picks the tray, delivers it to IO, releases it, retracts the
   empty tool and closes the internal IO door. `awaiting_operator` means access
   is ready. The tray may stay in IO or be removed and returned by the operator.
3. UI shows the item to take/add. Operator clears the opening and presses Done.
4. `POST /api/storage/bin-workflows/{id}/done` takes
   `{"target_item_in_bin": false}` for normal retrieval or `true` for storage.
   This explicit contents confirmation replaces the required scan stages and
   separate Store confirmation. Optional scans do not start motion.
5. `returning_bin` grips the tray directly in IO and returns it to its fixed
   home. Inventory changes finalize only after the physical return succeeds.
   Track workflow SSE updates or read `/api/storage/bin-workflows/active`.

The old `/return` endpoint and scan-wait state names are replaced. Deploy the OS
and UI branches together. Unavailable tray restoration still uses explicit
`confirmed_item_ids`; interrupted motions retain admin recovery/reconciliation.
Finish existing separate-bin workflows before upgrading. Legacy scan-wait
workflows are routed to recovery on startup, not silently resumed.

Both FastAPI mock mode and the UI's independent mocking mode support this flow.
The API mock completes delivery/return immediately; the UI mock animates each
motion. Neither requires a scanner to complete the normal tray workflow.

## First machine validation

The prototype uses `PICKUP_POSITION_L` for both IO handoff and re-grip, with
`STAGE_EXTENSION_IO_BOX` and `Z_EXTENSION_IO`. It deliberately uses zero stage
nudge and no `PICKUP_POSITION_H` item-transfer movement. These existing calibrated
positions must support the bottomed tray and allow gripper re-engagement; software
tests cannot establish the physical clearances or reliable tray seating.

Start with an empty bottomed tray and verify support at the handoff pose before
release, empty tool clearance, door clearance, and re-grip at the same pose.
Then verify an operator-removed/replaced tray seats repeatably, followed by a
loaded retrieval and storage. Confirm that Done stays unavailable with no tray,
that hands in the opening trigger the existing curtain guard, and that an
interrupted return requires recovery without accidentally fetching a second tray.

Use the device's development branch and repo-managed hardware startup flow.
Do not deploy or run motion from a dirty remote checkout. Configuration and home
cell reservation survive this change; no inventory/database migration is needed.
