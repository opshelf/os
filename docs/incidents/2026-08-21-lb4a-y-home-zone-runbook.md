# lb4a Y home-zone input mapping runbook

Status: harness prepared and locally verified. Do not run an active stage without
onsite restraint and E-stop supervision.

## Purpose

Verify the electrically confirmed PCA9535_0 inputs P04 (`YMIN`) and P05
(`YMAX`), and measure YMIN's raw polarity and approximate assertion/clear
position. The electrical mapping is PCF8575_0 P00/P01 to PCA9535_0 P04/P05.
This is a prerequisite to using YMIN as an independent guard around the
sensorless hard-stop homing sequence.

The confirmed scan records each limit twice: once as the raw PCA9535 port bit
and once through the same `GpioManager`/`DigitalInput` application path used by
the light curtain and cabinet-door sensors. The application value includes the
configured logical inversion; the raw value does not.

## Fixed safety envelope

The active harness:

- requires the normal API to be stopped and takes an exclusive diagnostic lock;
- accepts only the confirmed CL2B-RS507 Y profile at Modbus unit 2;
- requires dedicated brake output SO2 `0x24`, delays `250/250 ms`, peak current
  `60`, control mode `2`, command PPR `10000`, and encoder CPR `4000`;
- requires Y to start within `-1..1 mm`;
- moves at 10 RPM and acceleration 100;
- moves once from near zero to exactly 100 mm;
- returns through fixed targets `50, 20, 10, 5, 2, 1, 0.5, 0.25, 0 mm`,
  stopping as soon as raw P04 returns to its initial state;
- never targets below zero or above 100 mm;
- never homes, changes SO2, saves EEPROM, or accepts arbitrary motion values.

If P04 does not clear at 100 mm or does not return by zero, the harness records
that result and stops without commanding motion below zero. P05 and all raw
input bytes are captured at every stopped position.

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

Run the confirmed limit scan only after the onsite engineer reconfirms readiness:

```sh
./scripts/run_y_home_zone_stage.sh \
  scan-confirmed-limits \
  ONSITE-RESTRAINT-AND-ESTOP-READY
```

Expected physical behavior: one slow upward move to 100 mm, followed by fixed
downward moves that stop when P04 returns to its near-zero state or at zero.
The brake may make a subtle normal engagement sound between steps. There must
be no hard-stop homing impact, violent vibration, or load drop.

Record from `confirmed_limit_scan_complete`:

- `y_min_cleared_at_100` and `y_min_reasserted`;
- `y_min_reassert_position_mm`;
- `y_max_changed_at_100` (normally false);
- `gpio_at_home`, `gpio_at_100`, and `gpio_final`;
- `application_gpio_at_home`, `application_gpio_at_100`, and
  `application_gpio_final`;
- onsite sound, vibration, and motion observations.

## Test order and comparison

Run lb4a-2 first because it has not shown the violent burst. Then repeat the
same sequence on lb4a-1. Do not assume equivalence unless both traces show:

- raw pin 4 changes away from the lower stop and returns on approach;
- the same raw polarity;
- reasonably similar clear/reassert positions and hysteresis;
- no unrelated raw input changes correlated with Y motion.

A mapping/polarity/position difference supports a machine-specific wiring,
switch, or mechanical cause. Matching traces support using P04 as a common
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
