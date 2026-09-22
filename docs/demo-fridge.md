# Demo fridge configuration

The fridge integration is available when the API is running in `demo` or
`development` mode and `demo.fridge.enabled` is true in that mode's active
configuration. Production and mock modes ignore the fridge settings.

Configure the fridge under the top-level `demo` object. The values below are
illustrative; measure the installed door travel, center, cell coordinate, and
required stage reach before enabling it.

For initial motor calibration, configure only the motor and leave `enabled`
false. In demo or development mode this exposes `fridge_door` in the
`FRIDGE MOTORS` section without enabling fridge storage workflows or automatic
startup homing. Every fridge home operation moves from the detected endpoint to
the configured center, or to the midpoint of travel until positions are saved.

```json
{
  "demo": {
    "fridge": {
      "enabled": false,
      "motor": {
        "motor_name": "fridge_door",
        "motor_id": 20,
        "Ip": 36,
        "rpm": 200,
        "dr": 1,
        "mpr": 80,
        "accel": 3000,
        "ret_rpm": 64,
        "lim": 814
      }
    }
  }
}
```

The 814 mm limit above is the mechanically verified fridge door travel on
lb2-3. Its initial positions are left 2.5 mm, center 407 mm, and right 811.5 mm.
Use the mechanically verified limit for other installations.

```json
{
  "demo": {
    "cell_map_path": "data/demo.json",
    "fridge": {
      "enabled": true,
      "motor": {
        "motor_name": "fridge_door",
        "motor_id": 20,
        "Ip": 36,
        "rpm": 200,
        "dr": 1,
        "mpr": 80,
        "accel": 3000,
        "ret_rpm": 64,
        "lim": 814
      },
      "door_positions_mm": {
        "open_left": 2.5,
        "closed": 407,
        "open_right": 811.5
      },
      "cells": [
        {
          "location": {
            "side": "right_module",
            "shelf_idx": 0,
            "cell_idx": 0
          },
          "stage_extension_mm": 95
        }
      ]
    }
  }
}
```

Copy the installed demo X-axis electrical and motion profile rather than relying
on the illustrative motor values. The three door positions must be distinct and
within the fridge motor limits, with `closed` between the two open positions.
Every fridge stage extension must be within the stage motor limit; increase the
demo stage `lim` only to the mechanically verified reach.

The door direction is derived from the cell and the installed X-axis travel:
a cell left of the X-axis midpoint opens the door to the right and a cell right
of the midpoint opens it to the left. The runtime opens the door once the gantry
is positioned at the cell and before the tool rotates or extends; rotation may
itself extend the stage to its rotation-safe extension. The door then closes
after the tool retracts.

For the same off-center rotation compensation used on lb2-1, enable the existing
stage setting in the demo configuration:

```json
{
  "motor_name": "stage",
  "rotation_safe_stage_extension_enabled": true,
  "rotation_safe_stage_extension_mm": 8.5
}
```

Use the existing motors page to probe motor ID 20. Installation engineers can
set, test, save, and reset the three named positions at `/calibration/fridge`.
The `fridge_door` home action homes against the configured end and then always
moves to center. Fridge door move and home actions are rejected while the tool
is not fully retracted.
Every absolute fridge-door move, including center, uses the configured `rpm`.
Only the end-stop homing phase (and its internal approach) uses `Motor.home`'s
existing `home_rpm` or `ret_rpm` behavior.

In the demo UI, Ctrl+Left opens the door to the left position, Ctrl+Right opens
it to the right position, and either Ctrl+Up or Ctrl+Down closes it at center.
These shortcuts are active only in demo or development operating mode while the
UI is unlocked, and are ignored while the user is typing in an input.

Each fridge barcode remains mapped to its configured grid location through
`data/demo.json`; the fridge configuration is location-based so retrieval by
location follows the same motion path.
