# Config schema migrations

`schema_version` is a top-level positive integer, independent of the existing
application/config `version` string. A missing `schema_version` means **1**, the
provisioning schema used before this release. The current schema is **4**.
Version 1 identifies field meanings, not a canonical set of machine values.

The application requires the current schema. It does not migrate on startup and
refuses older or future schemas before parsing nested config models. Do not fix a
startup error by changing only `schema_version`: old field names would then lose
their meaning. Firmware predating this mechanism cannot enforce this check; a
rollback to that firmware must restore its original config too.

## Upgrade a device

Use the new release's migration runner and dependencies. From a source checkout,
prepare its environment with `cd openshelf_hardware && uv sync --all-packages`.
Preview from that directory:

```sh
uv run --no-sync python ../scripts/migrate_config.py data
```

The directory form selects only `config.json` and `config.*.json` immediately in
that directory, including production, development, demo and mock. It does not
recurse into config history or change storage grids, databases or scan recordings.
Individual files and multiple directories can also be supplied explicitly.

Stop the application through `./scripts/stop` from the repository root, then:

```sh
cd openshelf_hardware
uv run --no-sync python ../scripts/migrate_config.py data --write
```

On a device using containers, first obtain the matching released API image and
checkout, stop the stack, then run the new image without starting its server or
dependencies. From the device checkout:

```sh
docker compose -f docker-compose.yml run --rm --no-deps --pull never --entrypoint sh \
  -v "$PWD/scripts:/app/scripts:ro" api -lc \
  'cd /app/openshelf_hardware && uv run --no-sync python /app/scripts/migrate_config.py data'
```

Repeat with `--write` added after `data` once the preview is correct. Hardware
passthrough is unnecessary for this offline operation. Then restart via the
repo-managed flow, preserving the selected mode and hardware overlay, e.g.
`./scripts/start -d -h --mode development` on a development hardware device.

Every selected file is migrated in memory and validated against the current
`RootConfig` before any file is written. Unknown fields and numeric calibration
values are retained; model serialization is not used to rewrite the document.
An unrelated config validation failure must be reconciled before migration can
proceed. Migration does not invent geometry or fix tool/model reference mismatches.

Each changed file gets an exclusive, byte-for-byte `<filename>.schema-v1.bak`
backup (the number is its source schema). The original is replaced atomically,
preserving its owner and permissions. Already-current files are validated but not
rewritten. Conflicting old/new keys, duplicate JSON keys, symlinks, unsupported
versions and existing backup destinations are rejected. Output contains paths and
changed key names, not config values or secrets.

Replacement is atomic **per file**, not across the directory. If an I/O failure
interrupts a batch, inspect the reported files and backups before retrying. Keep
the application stopped. For rollback, restore the affected original config files
from their backups together with the prior firmware; do not lower only the version
number. Treat backups as private because they contain credentials.

At release time, migrate `../provision/config/config.json` with this same runner
and coordinate its schema-2 template with the schema-2 firmware. Do not ship a new
template to firmware predating this release. This worktree does not modify the
separate provisioning repository or live device configs.

## Schema 1 → 2

| Old key in `robot.grid.scan` | New key |
| --- | --- |
| `laser_scan_stage_extension_mm` | `laser_scan_pose_stage_mm` |
| `laser_scan_z_offset_from_place_mm` | `laser_scan_pose_z_delta_from_place_mm` |
| `left_xy_offset` / `right_xy_offset` | `left_gripper_xy_correction_mm` / `right_gripper_xy_correction_mm` |
| `left_z_offset_model` / `right_z_offset_model` | `left_wall_depth_to_gripper_z_model` / `right_wall_depth_to_gripper_z_model` |
| Model `stage_extension_mm` | `reference_stage_mm` |
| Model `z_place_mm` | `reference_z_place_mm` |
| Model `slope_per_mm` | `x_slope_mm_per_mm` |

The unused scan fields `z_extension_mm` and `angle_calibration_beam_zero_deg` are
removed. Known older scan aliases are also renamed by migration 2. Absent optional
fields remain absent; zero values remain zero.

The calculations are unchanged:

```text
scan stage = laser_scan_pose_stage_mm
scan Z = Z_EXTENSION_PLACE + laser_scan_pose_z_delta_from_place_mm
gripper X = detected centre X + laser_offset_x_mm + side_gripper_xy_correction_mm.x_mm
gripper Y = detected centre Y - laser_offset_y_mm + side_gripper_xy_correction_mm.y_mm
cell z_offset = wall depth + intercept_mm + x_slope_mm_per_mm * gripper X
```

Z-model reference fields describe the **shelf engagement** geometry, not the scan
pose. Their existing comparison against live tool parameters remains unchanged.
The two XY correction layers have not been combined.

## Schema 2 → 3

The laser scan block shipped many keys that no code path read. Migration 3 removes
them and nothing else:

| Removed key in `robot.grid.scan` | Why |
| --- | --- |
| `angle_calibration_method` | the method is `stage_stall` in the model itself |
| `angle_calibration_z_offset_{min,max,step}_mm`, `angle_calibration_z_offset_count` | legacy beam-angle sweep |
| `angle_calibration_rotation_{min,max}_deg` | superseded by the `*_stage_stall_rotation_*` pair |
| `angle_calibration_alpha_{min,max}_deg` | legacy beam-angle sweep |
| `angle_calibration_{coarse_step,fine_window,fine_step}_deg` | legacy beam-angle sweep |
| `angle_calibration_stage_stall_fall_threshold_mm`, `..._stop_after_fall_count` | only the standalone CLI argparse uses these |

No calibration value is moved, rewritten or defaulted by this step: the removed
keys influenced no calculation. Values are dropped, so the backup is the only
remaining copy.

## Schema 3 → 4

The scan block also carried its algorithm's tuning values, copied identically onto
every machine. Migration 4 removes them; the scan reads them from
`openshelf_os.laser_calibration.defaults` instead.

| Moved out of `robot.grid.scan` | Now |
| --- | --- |
| hole detection: `wall_depth_tolerance_mm`, `expected_hole_{width,height}_mm`, `hole_{width,height}_tolerance_mm`, `min_{vertical,horizontal}_wall_coverage_mm` | `laser_calibration.defaults` |
| sampling: `laser_sample_settle_ms`, `laser_samples_per_point` | `laser_calibration.defaults` |
| `z_offset_model_param_tolerance_mm` | `laser_calibration.defaults` |
| `z_stall_probe_*` probe geometry, speed and timeouts | `laser_calibration.defaults` |
| `angle_calibration_x_{min,step}_mm`, `angle_calibration_x_count` | `laser_calibration.defaults` |
| `angle_calibration_stage_stall_*` coarse/fine sweep, validation and probe settings, except the keys named below | `laser_calibration.defaults` |
| `angle_calibration_stage_stall_rotation_{min,max}_deg`, `angle_calibration_rotation_step_deg` | dropped: the stage-stall `coarse_*` keys always shadowed them, so a value set here never reached the sweep |

The promoted values are the ones every machine already carried or defaulted to, so
this step changes no calculation. It enforces that: a stored value that no longer
matches its promoted constant is refused, naming the affected keys without printing
their values, instead of silently retuning that machine. The step does remove the
ability to retune a single machine's scan through its config: change the constant
and release instead.

`fine_cell_scan_y_margin_mm` is the one tuning value the fleet does not agree on:
one machine stores 24.0 while the rest store null, which the getter treats as "use
the 6.0 code default". Promoting it would have silently retuned that machine, so
it stays in `robot.grid.scan` as a per-machine setting, with the code default as
the fallback.

The same applies to two angle-sweep keys, which is what the table row above excludes:
`angle_calibration_x_max_mm` and `angle_calibration_stage_stall_no_progress_timeout_s`
stay in `robot.grid.scan` because machines configure them differently.

## Add a migration

Add `v005_<description>.py` under
`openshelf_hardware/packages/openshelf_os/src/openshelf_os/config_migrations/`.
Its `migrate(config)` function accepts raw schema-4 data and returns the transformed
dictionary plus a list of changed field paths. Register it under destination `5`
in `runner.MIGRATIONS` and increment `config.schema.CURRENT_SCHEMA_VERSION`.
The runner advances the version only after each step succeeds. It rejects missing
steps and validates the complete result before persistence.

Never modify a released migration; append a new step. Test value preservation,
conflicts, optional fields, the whole migration chain, and startup rejection.
Update generated config defaults, fixtures, release notes and the provisioning
template for each new schema. Do not include secrets in migration diagnostics.
