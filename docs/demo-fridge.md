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
startup homing. Manual home establishes the endpoint but does not move to a
closed position until `door_positions_mm` is configured.

```json
{
  "demo": {
    "fridge": {
      "enabled": false,
      "motor": {
        "motor_name": "fridge_door",
        "motor_id": 20,
        "Ip": 36,
        "rpm": 500,
        "dr": 1,
        "mpr": 80,
        "accel": 500,
        "ret_rpm": 64,
        "lim": 950
      }
    }
  }
}
```

The 950 mm limit above applies to lb2-3's confirmed X-equivalent fridge axis;
use the mechanically verified limit for other installations.

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
        "rpm": 500,
        "dr": 0,
        "mpr": 80,
        "accel": 500,
        "ret_rpm": 64,
        "lim": 200
      },
      "door_positions_mm": {
        "open_left": 0,
        "closed": 100,
        "open_right": 200
      },
      "cells": [
        {
          "location": {
            "side": "right_module",
            "shelf_idx": 0,
            "cell_idx": 0
          },
          "door_open_direction": "left",
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

For the same off-center rotation compensation used on lb2-1, enable the existing
stage setting in the demo configuration:

```json
{
  "motor_name": "stage",
  "rotation_safe_stage_extension_enabled": true,
  "rotation_safe_stage_extension_mm": 8.5
}
```

Use the existing motors page to probe motor ID 20 and move it to measured targets.
When calibrated positions are present, the `fridge_door` home action homes against
the configured end and then moves to the explicit closed position. Fridge door
move and home actions are rejected while the tool is not fully retracted.

Each fridge barcode remains mapped to its configured grid location through
`data/demo.json`; the fridge configuration is location-based so retrieval by
location follows the same motion path.
