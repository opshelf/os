# lb4a Y home-zone input mapping runbook

Status: harness prepared and locally verified. Do not run an active stage without
onsite restraint and E-stop supervision.

## Purpose

Determine whether PCA9535_0 raw P00 is the Y-minimum home-zone switch on both
lb4a-1 and lb4a-2, and measure its raw polarity and approximate assertion/clear
positions. This is a prerequisite to using the switch as an independent guard
around the sensorless hard-stop homing sequence.

The engineer's table identifies P00 as `YMIN` and P01 as `YMAX`. The current
software defaults instead name PCA9535_0 pin 0 `x_limit_min`, pin 1
`x_limit_max`, pin 4 `y_limit_min`, and pin 5 `y_limit_max`. This runbook reads
raw P00 directly and records the configured names; it does not change that
mapping.

## Fixed safety envelope

The active harness:

- requires the normal API to be stopped and takes an exclusive diagnostic lock;
- accepts only the confirmed CL2B-RS507 Y profile at Modbus unit 2;
- requires dedicated brake output SO2 `0x24`, delays `250/250 ms`, peak current
  `60`, control mode `2`, command PPR `10000`, and encoder CPR `4000`;
- requires Y to start within `-1..1 mm`;
- moves at 10 RPM and acceleration 100;
- moves away from home in fixed 1 mm steps, never above 10 mm;
- returns in fixed 0.5 mm steps, stopping as soon as raw P00 returns to its
  initial state;
- never targets below the initial Y position;
- never homes, changes SO2, saves EEPROM, or accepts arbitrary motion values.

If P00 does not change by 10 mm, the harness stops and leaves Y at 10 mm. If it
changes while moving away but does not return before the initial position, the
harness stops without commanding motion below the initial position.

## Immediate stop conditions

The onsite engineer must cut motor power immediately for harsh noise or
vibration, brake/relay chatter, unexpected direction, any visible drop, a driver
alarm, or loss of remote communication during motion. Do not continue after a
stop condition.

## Common onsite gates

- Independently restrain/support the vertical load and clear the pinch/crush
  zone.
- Engineer at the E-stop with voice communication to the remote operator.
- Confirm the driver label is `CL2B-RS507` and motor is `CS-M234120B MS30`.
- Do not power-cycle the driver or save EEPROM between inspection and scan.
- Capture synchronized video/audio.
- Test one machine at a time. Review its trace before proceeding to the other.

Each command stores JSONL under `openshelf_hardware/data/y-home-zone/`.

## Per-machine sequence

The machine must be physically at its lower home position and its reported Y
position must be within `-1..1 mm`. Do not invoke the suspect already-homed
homing path to manufacture this state. If preparation requires homing, move Y
above the existing 5 mm approach point first and use only the already-observed
smooth normal homing path under supervision.

From a local shell:

```sh
../provision/remote MACHINE
```

On the device:

```sh
cd /opt/opshelf/repos/openshelf_os
git status --short --branch
git rev-parse HEAD
tr -d '[:space:]' < openshelf_hardware/data/.mode
./scripts/stop
```

First take a read-only snapshot. Expected physical motion: none.

```sh
./scripts/run_y_home_zone_stage.sh inspect
```

Review the trace before arming the scan. It must show stationary Y in
`-1..1 mm`, alarm 0, SO2 `0x24`, and `250/250 ms` delays.

Run the bounded scan only after the onsite engineer reconfirms readiness:

```sh
./scripts/run_y_home_zone_stage.sh \
  scan-zone \
  ONSITE-RESTRAINT-AND-ESTOP-READY
```

Expected physical behavior: several very small, slow upward moves totaling no
more than 10 mm, followed by small downward moves that stop at the switch-zone
edge. The brake may make a subtle normal engagement sound between steps. There
must be no hard-stop homing impact, violent vibration, or load drop.

Record from `home_zone_scan_complete`:

- `initial_pin0_high`, `clear_pin0_high`, and `reassert_pin0_high`;
- `clear_position_mm` and `reassert_position_mm`;
- `changed_pins` (it must contain pin 0);
- onsite sound, vibration, and motion observations.

## Test order and comparison

Run lb4a-2 first because it has not shown the violent burst. Then repeat the
same sequence on lb4a-1. Do not assume equivalence unless both traces show:

- raw pin 0 changes away from the lower stop and returns on approach;
- the same raw polarity;
- reasonably similar clear/reassert positions and hysteresis;
- no unrelated raw input changes correlated with Y motion.

A mapping/polarity/position difference supports a machine-specific wiring,
switch, or mechanical cause. Matching traces support using P00 as a common
software homing-zone guard but do not rule out a machine-specific brake,
coupling, hard-stop, or driver-current issue.

## After mapping

Do not repeat the violent already-homed hard-stop path. Review both traces, then
prepare the candidate software behavior:

1. If YMIN is asserted before homing, permit only a bounded low-speed move away.
2. Require YMIN to clear before issuing the sensorless home seek.
3. Refuse homing if it does not clear within the bounded distance/time.
4. Require YMIN to reassert after the seek before accepting Alarm 32 and setting
   zero.
5. Correct the existing approach-target predicate so being below the 5 mm
   approach point is not treated as already at that point.

Validate the clearance move alone, then a home from the clear state, then one
supervised restart/already-homed cycle on lb4a-1. Run the same candidate on
lb4a-2 as a regression check.

## Service restoration

Restore each device's original branch before restarting it. Use the active
mode recorded above and the repository-managed hardware startup path. For
example, after restoring the correct branch:

```sh
./scripts/start -d -h
```

Do not use `docker restart`. Confirm `/api/health` is healthy after restoration.
