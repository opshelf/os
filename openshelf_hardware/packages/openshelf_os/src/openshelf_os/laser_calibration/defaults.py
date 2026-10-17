"""Tuning constants for laser-aided calibration.

These values used to live in `robot.grid.scan` in `config.json`, where every machine
carried its own copy of the same number. They describe the scan algorithm, the shelf
fixtures and the scan plan rather than the individual machine, so they live here as
one source of truth and no longer appear in the persisted config.

What stays in `robot.grid.scan` is what genuinely differs per machine: the
laser-to-gripper offsets, the scan pose, the per-side XY corrections and wall-depth
z models, the scan bounds, and the handful of tuning values the fleet does not agree
on. See `docs/config-migrations.md`.
"""

from __future__ import annotations

# Hole detection. The engagement holes are cut into the same shelf plates on every
# cabinet, so the expected size and the tolerances around it are design constants.
WALL_DEPTH_TOLERANCE_MM = 2.0
EXPECTED_HOLE_WIDTH_MM = 26.0
EXPECTED_HOLE_HEIGHT_MM = 30.0
HOLE_WIDTH_TOLERANCE_MM = 10.0
HOLE_HEIGHT_TOLERANCE_MM = 10.0
MIN_VERTICAL_WALL_COVERAGE_MM = 10.0
MIN_HORIZONTAL_WALL_COVERAGE_MM = 30.0

# Laser sampling per scan point.
LASER_SAMPLE_SETTLE_MS = 50
LASER_SAMPLES_PER_POINT = 3

# A moving pass reads the laser and then the encoder, so each depth is assigned to a position
# read a fixed interval later. The distance error is feed x latency: fitting that delay against
# hole-edge midpoints on lb4a-2 at 40mm/s and 79mm/s gives 42ms (mean residual <=0.03mm at both
# feeds; the per-hole scatter that remains is edge-detection noise, not delay). It scales with
# feed, so it is subtracted per sample from the measured velocity. Re-measure it per sensor with
# a stopped-vs-moving edge comparison before trusting the default, and store the result in
# `robot.grid.scan.flying_capture_latency_ms`; this is only the fallback when that is unset.
FLYING_CAPTURE_LATENCY_MS = 42.0

# A wall depth reading is turned into a gripper z offset by a per-machine model. The
# model records the tool geometry it was derived at; this is how far the live
# Z_EXTENSION_PLACE may drift before the model is refused as stale (stage drift is carried).
Z_OFFSET_MODEL_PARAM_TOLERANCE_MM = 0.2

# Z stall anchor probe: drive z down until the gripper pin stalls against the tray.
Z_STALL_PROBE_START_MM = 5.0
Z_STALL_PROBE_FAST_APPROACH_MM = 130.0
# How far short of the laser-predicted contact a predicted approach stops, at place speed, before
# creeping. Shelves have differed from the trusted intercept by up to ~2mm (lb4a-3, 2026-10-17).
Z_STALL_PROBE_APPROACH_MARGIN_MM = 6.0
Z_STALL_PROBE_TARGET_MM = 180.0
Z_STALL_PROBE_STEP_MM = 0.5
Z_STALL_PROBE_RPM = 20
Z_STALL_PROBE_NO_PROGRESS_S = 0.25
Z_STALL_PROBE_TIMEOUT_S = 60.0

# Legacy beam-angle sweep: the x positions the angle calibration probes along.
ANGLE_SWEEP_X_MIN_MM = 0.0
ANGLE_SWEEP_X_STEP_MM = 100.0
ANGLE_SWEEP_X_COUNT = 5

# Stage-stall angle calibration: rotate until the stage jams against the shelf, then
# refine. The sweep window is uniform, so it lives here; per-machine rotation centres
# come from the tool parameters, and `angle_calibration_stage_stall_no_progress_timeout_s`
# stays in `robot.grid.scan` because machines disagree on it.
STAGE_STALL_COARSE_MIN_DEG = 0.0
STAGE_STALL_COARSE_MAX_DEG = 5.5
STAGE_STALL_COARSE_STEP_DEG = 1.0
STAGE_STALL_COARSE_START_DEG = 4.5
STAGE_STALL_FINE_WINDOW_DEG = 1.5
STAGE_STALL_FINE_STEP_DEG = 0.25
# 0.0 means no fast approach: the probe starts directly at STAGE_STALL_START_MM.
STAGE_STALL_FAST_APPROACH_MM = 0.0
STAGE_STALL_VALIDATION_COUNT = 3
STAGE_STALL_VALIDATION_REPEATS = 3
STAGE_STALL_VALIDATION_RETRY_COUNT = 1
STAGE_STALL_COARSE_RETRY_COUNT = 1
STAGE_STALL_MIN_VALID_STAGE_POSITION_MM = 40.0
STAGE_STALL_VALIDATION_PLATEAU_TOLERANCE_MM = 0.75
STAGE_STALL_COARSE_VALIDATION_COUNT = 1
STAGE_STALL_VALIDATION_HIGH_SIDE_WINDOW_DEG = 0.75
STAGE_STALL_LOW_CONTACT_RETRY_COUNT = 1
STAGE_STALL_VALIDATION_MIN_VALID_SAMPLE_COUNT = 2
STAGE_STALL_PROBE_X_MM = 475.0
STAGE_STALL_START_MM = 3.0
# None means probe to the stage travel limit instead of a fixed target.
STAGE_STALL_TARGET_MM: float | None = None
STAGE_STALL_PROBE_STEP_MM = 0.5
STAGE_STALL_PROBE_RPM = 20
STAGE_STALL_FAST_APPROACH_MARGIN_MM = 2.0
STAGE_STALL_FAST_APPROACH_RPM = 20
STAGE_STALL_TIMEOUT_S = 30.0
STAGE_STALL_BACKOFF_MM = 8.0
