# lb4a-1 Y brake timing and first-home runbook

Status: prepared only. Run one stage at a time with onsite supervision.

## Scope

This runbook tests the remaining software-side suspects after the successful
`0xA1 -> 0x24` and 5 mm isolated test:

1. Change only the volatile brake release/engagement delays between 100 and
   250 ms and make three slow, ascending moves.
2. Start the post-brake application with automatic homing disabled, then compare
   the first Y-only home requested as soon as the API is ready with one requested
   after a fixed five-second settle.

No script saves driver EEPROM. The timing harness cannot change SO2, home, or
accept arbitrary registers, values, speed, acceleration, or targets. Its fixed
targets are 10, 15, and 20 mm, all below the separate 50 mm safety ceiling. The
first-home client cannot restart the API, modify `home_on_init`, acknowledge a
safety interruption, or enable a safety bypass. Immediately before homing it
also requires live Y at `-1..50 mm`, no current/queued command, no crash or
active E-stop, and every global/component safety bypass to be false.

True `home_on_init=true` is deliberately excluded: current startup homes all
enabled axes rather than only Y, so it is not an isolated Y test.

## Immediate stop conditions

The onsite engineer must cut motor power immediately for harsh noise or
vibration, brake/relay chatter, unexpected motion, downward movement, visible
load drop, a driver alarm, or loss of remote communication during motion. Do
not continue after any stop condition.

## Onsite and software gates

- Independently restrain/support the vertical load and keep the exclusion zone
  clear. The brake and E-stop are not the independent restraint.
- Engineer at the E-stop, with voice communication to the remote operator.
- Confirm the installed driver label is `CL2B-RS507` and motor is
  `CS-M234120B MS30`.
- Device must be in development mode, with a clean checkout of the reviewed
  diagnostic commit and `robot_app.home_on_init=false` in
  `openshelf_hardware/data/config.development.json`.
- Do not power-cycle the drive or save EEPROM between stages.
- Capture synchronized video/audio and, if available, SO2, relay-coil,
  brake-coil, and motor-current measurements.

From the local repository, open the remote device shell and keep it open:

```sh
../provision/remote lb4a-1
```

On the device, verify the exact branch, commit, cleanliness, mode, and homing
setting before continuing:

```sh
cd /opt/opshelf/repos/openshelf_os
git status --short --branch
git rev-parse HEAD
tr -d '[:space:]' < openshelf_hardware/data/.mode
rg -n 'home_on_init' openshelf_hardware/data/config.development.json
```

Stop the API through the repository-managed path. Expected physical movement
is none:

```sh
./scripts/stop
```

Every harness invocation prints and saves its trace under
`openshelf_hardware/data/y-brake-timing/`. Read-only inspections omit the
confirmation phrase. Every active stage requires the explicit onsite phrase.

## Part A: one-variable delay matrix

The expected initial volatile state is SO2 `0x24`, release 100 ms, engagement
100 ms. Confirm it without writes:

```sh
./scripts/run_y_brake_timing_stage.sh inspect-baseline
```

If this fails, do not write a value to manufacture the baseline. Record the
reported live state and pause.

### A1: release delay only (250/100 ms)

```sh
./scripts/run_y_brake_timing_stage.sh set-release-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh move-release-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh restore-release-100 ONSITE-RESTRAINT-AND-ESTOP-READY
```

The delay write should produce no movement. The move slowly raises Y from its
current position to exactly 10 mm at 10 RPM and acceleration 100. Expected:
brake fully releases, smooth upward motion, clean stop, brake engages without
chatter. Pause after every command and compare software and physical traces.

### A2: engagement delay only (100/250 ms)

```sh
./scripts/run_y_brake_timing_stage.sh set-engagement-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh move-engagement-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh restore-engagement-100 ONSITE-RESTRAINT-AND-ESTOP-READY
```

Expected motion is a smooth upward move from 10 to exactly 15 mm. The harness
refuses a non-ascending move or an unexpected delay profile.

### A3: both delays (250/250 ms)

The only permitted path to 250/250 first sets engagement to 250, then changes
release to 250:

```sh
./scripts/run_y_brake_timing_stage.sh set-engagement-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh set-both-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh move-both-250 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh restore-both-release-100 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh restore-engagement-100 ONSITE-RESTRAINT-AND-ESTOP-READY
./scripts/run_y_brake_timing_stage.sh inspect-baseline
```

Expected motion is a smooth upward move from 15 to exactly 20 mm. The final
inspection must confirm SO2 `0x24`, 100/100 ms, stationary Y, and no alarm.

If a write response is lost, the harness reads back once without retransmitting.
If both response and readback fail, do not repeat the command: inspect the live
profile first, because the write may have taken effect.

## Part B: first Y home after restart

Run only after Part A is fully normal. The final `inspect-baseline` trace is the
position evidence for the first restart. Copy its printed absolute trace path.

### B1: home immediately after API readiness

Start current post-brake software with hardware passthrough. `home_on_init=false`
must already be set; the client verifies it but refuses to change it:

```sh
./scripts/start -d -h
```

Expected during startup: driver configuration and relay/brake state changes may
be audible, but there must be no axis motion, harsh vibration, or chatter.

As soon as startup is launched, run the following with the actual trace path.
The client waits for development/ready, rechecks `home_on_init=false`, verifies
Y is unit 2, and reads live status to verify Y remains under 50 mm. Because
lb4a-1 has no cabinet doors or door sensors, the final confirmation authorizes
the client to enable only the `cabinet_doors` component bypass. It first requires
that cabinet doors are the only active source and that the light curtain, global
bypass, E-stop, crash state, and command queue are safe. It then repeats the
full gate immediately before requesting only the existing Y-axis home endpoint:

The tool camera is not configured on lb4a-1 and is expected to report
`no frames within 15s`. If that timeout changes overall API readiness to
`degraded` during the settled test, the client accepts it only when `tool_camera` is the sole
failed component with that exact reason and every other component is ready.

```sh
OPENSHELF_DIAGNOSTIC_PIN=1111 ./scripts/run_y_first_home_stage.sh \
  home-immediate \
  openshelf_hardware/data/y-brake-timing/REPLACE-WITH-TRACE.jsonl \
  ONSITE-RESTRAINT-AND-ESTOP-READY \
  CABINET-DOORS-NOT-INSTALLED-AUTHORIZE-BYPASS
```

Expected physical motion: Y moves downward from at most 20 mm to its home
sensor/stop using the application's normal Y homing procedure, then stops. No
other axis is requested to home. Stop immediately for vibration or abnormal
sound.

### B2: home with a fixed five-second settle

After B1, stop the application and create fresh read-only position evidence.
Post-brake startup writes the configured 250/250 ms delays, so inspect that
profile:

```sh
./scripts/stop
./scripts/run_y_brake_timing_stage.sh inspect-both-250
```

Start again, then invoke the settled variant with the new trace path:

```sh
./scripts/start -d -h
OPENSHELF_DIAGNOSTIC_PIN=1111 ./scripts/run_y_first_home_stage.sh \
  home-after-5s \
  openshelf_hardware/data/y-brake-timing/REPLACE-WITH-NEW-TRACE.jsonl \
  ONSITE-RESTRAINT-AND-ESTOP-READY \
  CABINET-DOORS-NOT-INSTALLED-AUTHORIZE-BYPASS
```

This follows the same ready gate, then waits exactly five additional seconds
before repeating the gate and requesting one Y home. Compare B1 and B2 audio,
vibration, brake/relay timing, current, and JSONL output.

## Closeout

After the last observation, stop the application unless the test lead explicitly
requests normal service restoration:

```sh
./scripts/stop
```

Preserve the exact commit hash, all JSONL traces, container logs, electrical
measurements, and onsite observations. Do not proceed to `home_on_init=true`,
EEPROM changes, or a power cycle from this runbook.
