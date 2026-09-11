# Multi-item trays

A bottomed tray is the container. `storage.bins` config, database bin identities,
item membership and fixed `home_location` remain the existing storage contract.
Replace the separately carried bin with a compatible bottomed tray at that home.
Ordinary single-item transfers continue to use the original IO sequence.
Each home cell can optionally define a subdivided tray as described below.

## Operator/API flow

1. `POST /api/storage/bin-workflows` with `operation`, `upca`, and (for explicit
   multi-item storage) `storage_mode: "bin"` reserves the tray and starts delivery.
2. `retrieving_bin` picks the tray, delivers it to IO, releases it, retracts the
   empty tool and closes the internal IO door. `awaiting_operator` means access
   is ready. The tray may stay in IO or be removed and returned by the operator.
3. UI shows the item to take/add. `POST /api/storage/bin-workflows/{id}/close`
   completes that item instruction, updates membership, and leaves the tray outside.
4. Scan the outside tray, or use its known barcode in a guided flow, to call
   `POST /api/storage/bin-workflows/returns`. This opens an independent return
   interaction with all known contents selected by default. Closing this interaction
   cancels it without changing contents or moving hardware.
5. With the tray in IO, `POST /api/storage/bin-workflows/{id}/store` with
   `confirmed_item_ids` starts `returning_bin`. The robot grips the tray directly
   in IO and returns it to its fixed home. Track workflow SSE updates or read
   `/api/storage/bin-workflows/active`.

The old `/return` endpoint, scan-wait states, and global recovery/reconciliation
flow are replaced. Deploy the OS and UI branches together. Finish existing
separate-bin workflows before upgrading. Startup marks interrupted legacy motion
as failed and leaves its tray outside; a recorded physical return is finalized
without replaying motion. Scan the outside tray to start an independent return.

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
interrupted return leaves the tray outside without accidentally fetching a second tray.

Use the device's development branch and repo-managed hardware startup flow.
Do not deploy or run motion from a dirty remote checkout. Configuration and home
cell reservations survive this change. Startup migrates the database to add nullable
layout and compartment fields; existing unpartitioned inventory is preserved.


## Subdivided trays

Configure each tray through its `storage.bins` entry. For example:

```json
{
  "barcode": "900000000001",
  "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
  "inner_dimensions": {"width": 200, "length": 160, "height": 100},
  "compartment_layout": {"rows": 2, "columns": 4}
}
```

Rows run from back to front; columns run left to right in the operator's top view
at the IO box. Compartment 1 is top left (back left), followed by 2–4 across the
first row and 5–8 across the second. Keep that orientation when removing and
replacing a tray, and use the same orientation when printing the numbers.

One item occupies one compartment. Compartments are equal rectangles: width is
tray inner width / columns, length is inner length / rows, and height is the tray
inner height. The example allows a 50 × 80 mm footprint and 100 mm height.
An item may rotate 90° horizontally; items cannot span compartments or stack.
Use optional `max_item_dimensions: {"width": 48, "length": 78}` to account for
real divider thickness and clearance; it further caps the usable footprint.
`max_items` defaults to rows × columns, and may be reduced but cannot exceed it.
Rows and columns must be integers from 1 to 32.

Allocation checks item dimensions, available compartments and existing access
rules. It prefers a compatible nonempty tray, then the smallest compartment
volume, then home-cell order. Within a tray it reserves the first free number.
The selected number is persisted on the workflow and committed to membership
when the item instruction is completed; retrieval frees that number without
renumbering any other item. Independent returns preserve known positions and
reject conflicting or unknown assignments instead of guessing.

The API returns `compartment_layout` and `compartment_dimensions` on bin summaries,
`compartment` on each member, and the target `compartment` on the workflow.
Return `contents` also includes each candidate's known compartment. The UI uses
these fields for placement/retrieval instructions and its highlighted top view.
Both API mock mode and the independent UI mock keep the same numbering semantics.

Omit `compartment_layout` to retain an unpartitioned legacy bin. Before adding or
changing a layout, dimensions, capacity or home cell, empty the tray and finish
its active workflow with the tray at home. This prevents existing contents from
being silently assigned different physical positions. Configure the layout
before storing the first item; startup rejects unsafe configuration changes.
