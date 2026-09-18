#!/usr/bin/env python3
"""Probe the shelf plate beside reference cells and fit this side's z delta model.

This is the partial test for the z stall method: it runs exactly the probes the automatic
calibration runs, on whatever cells you point it at, so a machine can be checked without a
full scan and without writing anything. `--plan-only` prints the probes it would use and what
the hand-calibrated cells imply, and moves nothing at all.

The probe presses the shelf insert beside a cell's hole, so the load it applies is the load the
shelf latch has to hold. Every point is stalled twice and a pair that disagrees is dropped, and
any stage back-drive aborts the run: those are the two symptoms of the rack moving instead of
the tool meeting a wall.

Run it with the api container stopped, the way the other laser scripts run, so the robot's
serial ports are free.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
	REPO_ROOT / "openshelf_hardware/packages/openshelf_os/src",
	REPO_ROOT / "openshelf_hardware/packages/hardware/src",
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

import openshelf_os.laser_calibration as laser_module
from openshelf_os.common import CONFIG_PATH, load_config_file
from openshelf_os.runtime_state import RuntimeStatePaths, paths_for_config_file
from openshelf_os.storage import StorageModuleSide


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Probe the shelf plate and fit the z delta model, without a full calibration."
	)
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--module", choices=["left", "right"], required=True)
	parser.add_argument(
		"--from-run",
		type=Path,
		help="Run directory to take the detected cells from; defaults to the newest saved run.",
	)
	parser.add_argument(
		"--from-grid",
		action="store_true",
		help=(
			"Take the probe targets from the stored calibration points instead of a scan frame. Use "
			"this when the rack has moved since the last scan: the stored points are what the "
			"operator dialled in. Wall depths still come from a run's sweep."
		),
	)
	parser.add_argument("--shelf", type=int, help="Probe only this shelf.")
	parser.add_argument(
		"--cell",
		action="append",
		default=[],
		metavar="SHELF:CELL",
		help="Probe only this cell; repeatable.",
	)
	parser.add_argument(
		"--exclude",
		action="append",
		default=[],
		metavar="SHELF:CELL",
		help=(
			"Never probe this cell; repeatable. Tray homes and cells holding an item are probeable "
			"- a tray offers the same divider to the laser as an empty cell - so use this only to "
			"keep the probe off a specific opening."
		),
	)
	parser.add_argument(
		"--plan-only",
		action="store_true",
		help="Report the probes and the reference datum without moving anything.",
	)
	parser.add_argument(
		"--shelves",
		type=int,
		help=(
			"How many shelves to sample, spread over the module (default from "
			"z_stall_probe_shelves). 3 probes bottom, middle and top shelf; a larger number "
			"samples nearly every shelf, which is the strongest validation but the slowest run."
		),
	)
	parser.add_argument("--repeat-count", type=int, help="Override z_stall_probe_repeats.")
	parser.add_argument(
		"--repeat-tolerance-mm",
		type=float,
		help="Override z_stall_probe_repeat_tolerance_mm.",
	)
	parser.add_argument("--shutdown-action", choices=["stop", "shutdown"], default="stop")
	return parser


def parse_cells(raw_cells: list[str]) -> set[tuple[int, int]] | None:
	if len(raw_cells) == 0:
		return None

	cells: set[tuple[int, int]] = set()

	for raw_cell in raw_cells:
		parts = raw_cell.split(":")

		if len(parts) != 2:
			raise ValueError("--cell must be SHELF:CELL")

		cells.add((int(parts[0]), int(parts[1])))

	return cells


def run_side(run_dir: Path) -> str | None:
	metadata_path = Path(run_dir) / "metadata.json"

	try:
		return laser_module.load_json_file(metadata_path).get("side")
	except Exception:
		return None


def newest_run_dir(config_path: Path, side: StorageModuleSide) -> Path:
	"""Newest saved run for this side, since a machine scans each side separately."""
	scans_dir = paths_for_config_file(config_path).calibration_dir
	candidates = [
		path
		for path in scans_dir.iterdir()
		if path.is_dir() and (path / "metadata.json").exists() and run_side(path) == side.value
	]

	if len(candidates) == 0:
		raise ValueError(
			f"no saved {side.value} runs with metadata under {scans_dir}; pass --from-run"
		)

	return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_motor_limits(config: Any, motor_name: str) -> tuple[float | None, float | None]:
	motor = next(
		(
			entry
			for entry in config.robot.tool.motors
			if getattr(entry, "motor_name", None) == motor_name
		),
		None,
	)

	if motor is None:
		return None, None

	upper_mm = getattr(motor, "lim", None)
	lower_mm = getattr(motor, "lower_lim", None)

	return (
		None if upper_mm is None else float(upper_mm),
		None if lower_mm is None else float(lower_mm),
	)


def resolve_probe_reach(
	config: Any,
	*,
	side: StorageModuleSide,
	scan_config: Any,
) -> tuple[float | None, float | None]:
	"""The X travel the carriage has for a probe, in the tool frame the probe is aimed from.

	Planning and candidate selection only: `run_z_stall_anchor` applies the xy offset and the
	probe shift itself, so the run hands it the raw axis limits.
	"""
	xy_offset = laser_module.get_scan_xy_offset(scan_config=scan_config, side=side)
	offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])
	shift_mm = float(scan_config.z_stall_probe_x_shift_mm)
	upper_mm, lower_mm = resolve_motor_limits(config, "x_axis")

	return (
		None if upper_mm is None else float(upper_mm) - offset_x_mm - shift_mm,
		None if lower_mm is None else float(lower_mm) - offset_x_mm - shift_mm,
	)


def resolve_scan_config(config: Any, args: argparse.Namespace) -> Any:
	scan_config = config.robot.grid.scan
	overrides: dict[str, Any] = {}

	if args.repeat_count is not None:
		overrides["z_stall_probe_repeats"] = int(args.repeat_count)

	if args.repeat_tolerance_mm is not None:
		overrides["z_stall_probe_repeat_tolerance_mm"] = float(args.repeat_tolerance_mm)

	if len(overrides) == 0:
		return scan_config

	return scan_config.model_copy(update=overrides)


def report_exclusions(excluded_cells: set[tuple[int, int]]) -> None:
	if len(excluded_cells) == 0:
		return

	print(
		f"excluded {len(excluded_cells)} cell(s) from probing: "
		+ ", ".join(f"{shelf_idx}:{cell_idx}" for shelf_idx, cell_idx in sorted(excluded_cells))
	)


def report_plan(
	*,
	probes: list[tuple[Any, float]],
	shelves_to_probe: int,
	manual_reference: Any,
	side: StorageModuleSide,
	scan_config: Any,
	excluded_cells: set[tuple[int, int]],
	xy_offset: tuple[float, float] | None,
) -> None:
	# Where the tip goes, not just where the opening is: the probe aims at the divider beside the
	# hole, shifted by the tool's xy offset and by z_stall_probe_x_shift_mm.
	offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])
	offset_y_mm = 0.0 if xy_offset is None else float(xy_offset[1])
	shift_mm = float(scan_config.z_stall_probe_x_shift_mm)
	probe_count = len(probes)
	print(
		f"probe candidates ({probe_count}): one opening on each of {shelves_to_probe} shelf(s) spread "
		f"over the module, then one per remaining shelf as a reserve. Probing stops once the samples "
		f"span {laser_module.Z_DELTA_TILT_MIN_X_SPAN_MM:.0f}mm."
	)
	for cell, wall_depth_mm in probes:
		print(
			f"  {cell.shelf_idx}:{cell.cell_idx} probe_x={float(cell.tool_x_mm) + offset_x_mm + shift_mm:.2f} "
			f"probe_y={float(cell.tool_y_mm) + offset_y_mm:.2f} "
			f"(hole tool_x={float(cell.tool_x_mm):.2f} tool_y={float(cell.tool_y_mm):.2f}) "
			f"wall_depth_mm={float(wall_depth_mm):.3f}"
		)

	if manual_reference is None:
		print("reference datum: none (mark a cell manual with 'keep manual' before probing)")
		return

	print(
		f"reference datum: {manual_reference.sample_count} cell(s) imply intercept "
		f"{'none' if manual_reference.intercept_mm is None else f'{float(manual_reference.intercept_mm):.3f}'}mm "
		f"(agreement tolerance {float(scan_config.z_stall_manual_agreement_mm):.2f}mm)"
	)
	for sample in manual_reference.samples:
		print(
			f"  {sample.shelf_idx}:{sample.cell_idx} manual_x={float(sample.manual_x_mm):.2f} "
			f"manual_y={float(sample.manual_y_mm):.2f} wall_depth_mm="
			f"{'none' if sample.wall_depth_mm is None else f'{float(sample.wall_depth_mm):.3f}'} "
			f"intercept_mm="
			f"{'none' if sample.derived_intercept_mm is None else f'{float(sample.derived_intercept_mm):.3f}'} "
			f"captured_stage_mm="
			f"{'none' if sample.z_captured_stage_mm is None else f'{float(sample.z_captured_stage_mm):.2f}'}"
		)

	if manual_reference.z_capture_conditions_missing > 0:
		print(
			"  warning: "
			f"{manual_reference.z_capture_conditions_missing} reference cell(s) were saved before "
			"the capture stage was recorded, so their stage extension could not be checked"
		)


def report_result(*, result: Any, manual_reference: Any, scan_config: Any, side: StorageModuleSide) -> int:
	print(
		f"fit: source={result.fit_source} samples={result.sample_count} "
		f"x_span_mm={float(result.x_span_mm):.1f} intercept_mm={float(result.intercept_mm):.3f} "
		f"slope_per_mm={float(result.slope_per_mm):.5f}"
	)
	print(
		f"geometry: stage_extension_mm={float(result.stage_extension_mm):.2f} "
		f"z_place_mm={float(result.z_place_mm):.2f} rotation_deg={float(result.rotation_center_deg):.3f}"
	)
	for sample in result.samples:
		repeat = "none" if sample.z_contact_repeat_mm is None else f"{float(sample.z_contact_repeat_mm):.3f}"
		print(
			f"  probe {sample.shelf_idx}:{sample.cell_idx} x={float(sample.probe_x_mm):.2f} "
			f"wall_depth_mm={float(sample.wall_depth_mm):.3f} z_contact_mm={float(sample.z_contact_mm):.3f} "
			f"repeat_mm={repeat} delta_mm={float(sample.delta_mm):.3f}"
		)
	for dropped in result.dropped_repeats:
		print(f"  dropped (repeat moved): {dropped}")
	for skipped in result.skipped:
		print(f"  skipped: {skipped}")

	model = {
		"intercept_mm": round(float(result.intercept_mm), 3),
		"slope_per_mm": float(result.slope_per_mm),
		"stage_extension_mm": float(result.stage_extension_mm),
		"z_place_mm": float(result.z_place_mm),
	}
	side_name = "left" if side == StorageModuleSide.LEFT else "right"
	print(f"model for robot.grid.scan.{side_name}_z_offset_model: {model}")

	configured_model = laser_module.get_scan_z_offset_model(scan_config=scan_config, side=side)
	configured_slope_mm = 0.0 if configured_model is None else float(configured_model.slope_per_mm)
	agreement_mm = laser_module.z_model_datum_agreement_mm(
		intercept_mm=float(result.intercept_mm),
		slope_per_mm=float(result.slope_per_mm),
		configured_slope_per_mm=configured_slope_mm,
		manual_reference=manual_reference,
	)

	if manual_reference is None or agreement_mm is None:
		print("verdict: no reference datum, so the fit is unverified")
		return 1

	tolerance_mm = float(scan_config.z_stall_manual_agreement_mm)
	agreement_ok = agreement_mm <= tolerance_mm
	datum_cells = ", ".join(
		f"{sample.shelf_idx}:{sample.cell_idx}" for sample in manual_reference.samples
	)
	print(
		f"verdict: agreement with the reference datum {agreement_mm:.3f}mm (tolerance {tolerance_mm:.2f}mm) "
		f"judged at manual cell(s) [{datum_cells}], where the two models put them "
		f"(probe slope {float(result.slope_per_mm):.5f} vs datum slope {configured_slope_mm:.5f}) - "
		f"{'within tolerance' if agreement_ok else 'OUT OF TOLERANCE'}"
	)

	stage_ok = True

	if manual_reference.z_captured_stage_mm is not None:
		stage_delta_mm = abs(float(manual_reference.z_captured_stage_mm) - float(result.stage_extension_mm))
		pin_tolerance_mm = float(scan_config.z_offset_model_param_tolerance_mm)
		stage_ok = stage_delta_mm <= pin_tolerance_mm
		print(
			f"           datum captured at stage {float(manual_reference.z_captured_stage_mm):.2f}mm vs probe "
			f"{float(result.stage_extension_mm):.2f}mm (delta {stage_delta_mm:.2f}mm, pin tolerance "
			f"{pin_tolerance_mm:.2f}mm) - "
			+ (
				"comparable"
				if stage_ok
				else "NOT comparable: the datum was saved at a different stage extension"
			)
		)

	return 0 if agreement_ok and stage_ok else 1


async def main() -> int:
	args = build_parser().parse_args()
	side = StorageModuleSide.LEFT if args.module == "left" else StorageModuleSide.RIGHT
	cells = parse_cells(args.cell)
	config = load_config_file(args.config)
	scan_config = resolve_scan_config(config, args)
	runtime_paths = RuntimeStatePaths.current()
	grid_path = runtime_paths.resolve_storage_path(config, config_path=args.config)
	grid = laser_module.load_grid(grid_path)

	run_dir = args.from_run if args.from_run is not None else newest_run_dir(Path(args.config), side)
	metadata_path = Path(run_dir) / "metadata.json"
	metadata = laser_module.load_json_file(metadata_path)
	saved_side = metadata.get("side")

	if args.from_run is not None and saved_side is not None and saved_side != side.value:
		raise ValueError(
			f"--from-run holds a {saved_side} run but --module is {args.module}; probes come from the "
			"side being probed"
		)

	print(f"run: {metadata_path}")

	candidates = laser_module.load_candidates_from_metadata(metadata)

	if args.from_grid is True:
		# Wall depths have no cell indices of their own: they are matched to the openings the run
		# recorded, so the run's detections are the map between the two.
		wall_depths = laser_module.wall_depths_by_cell(
			analysis=laser_module.build_probe_analysis(
				metadata=metadata, metadata_path=metadata_path
			),
			candidates=candidates,
		)
		print(
			f"positions: stored calibration points ({len(wall_depths)} sweep wall depths available)"
		)
		analysis = laser_module.build_probe_analysis_from_grid(
			grid=grid,
			side=side,
			scan_config=scan_config,
			wall_depths=wall_depths,
			shelf_idx=args.shelf,
			cells=cells,
		)
	else:
		analysis = laser_module.build_probe_analysis(metadata=metadata, metadata_path=metadata_path)
		if args.shelf is not None or cells is not None:
			analysis = laser_module.restrict_analysis(analysis, shelf_idx=args.shelf, cells=cells)

	if len(analysis.shelves) == 0:
		print("nothing to probe: the run has no detected cells in that scope")
		return 1

	z_axis_limit_mm, _z_axis_lower_limit_mm = resolve_motor_limits(config, "z_axis")
	x_axis_limit_mm, x_axis_lower_limit_mm = resolve_motor_limits(config, "x_axis")
	scanned_cells = {
		(int(cell.shelf_idx), int(cell.cell_idx))
		for shelf in analysis.shelves
		for cell in shelf.cells
	}
	excluded_cells = laser_module.excluded_probe_cells(
		grid=grid, side=side, scanned_cells=scanned_cells
	)
	manually_excluded = parse_cells(args.exclude) or set()
	excluded_cells |= manually_excluded
	report_exclusions(excluded_cells)
	manual_reference = None
	try:
		manual_reference = laser_module.derive_reference_offsets(
			grid=grid,
			analysis=analysis,
			side=side,
			scan_config=scan_config,
			shelf_idx=args.shelf,
			cell_idx=None if cells is None or len(cells) != 1 else sorted(cells)[0][1],
		)
	except ValueError as exc:
		# A probe without a datum is still worth running: it measures the geometry, it just
		# cannot be called correct until a hand-calibrated cell backs it up.
		print(f"reference datum unavailable: {exc}")
	probe_wall_depths = laser_module.wall_depths_by_cell(analysis=analysis, candidates=candidates)
	max_probe_tool_x_mm, min_probe_tool_x_mm = resolve_probe_reach(
		config, side=side, scan_config=scan_config
	)
	shelves_to_probe = int(
		getattr(scan_config, "z_stall_probe_shelves", 3)
		if args.shelves is None
		else args.shelves
	)
	probes = laser_module.select_z_stall_probes(
		analysis,
		probe_wall_depths,
		max_probe_tool_x_mm,
		min_probe_tool_x_mm,
		excluded_cells=excluded_cells,
		shelves_to_probe=shelves_to_probe,
	)

	if len(probes) == 0:
		print(
			"nothing to probe: every detected cell in scope is excluded (disabled, hand-calibrated "
			"or --exclude), has no sweep wall depth, or sits past the carriage travel"
		)
		return 1

	if args.plan_only is True:
		report_plan(
			probes=probes,
			shelves_to_probe=shelves_to_probe,
			manual_reference=manual_reference,
			side=side,
			scan_config=scan_config,
			excluded_cells=excluded_cells,
			xy_offset=laser_module.get_scan_xy_offset(scan_config=scan_config, side=side),
		)
		return 0

	async def didRunProbe(robot: Any) -> Any:
		return await laser_module.run_z_stall_anchor(
			tool=robot._tool,
			analysis=analysis,
			scan_config=scan_config,
			side=side,
			excluded_cells=excluded_cells,
			x_axis_limit_mm=x_axis_limit_mm,
			x_axis_lower_limit_mm=x_axis_lower_limit_mm,
			z_axis_limit_mm=z_axis_limit_mm,
			xy_offset=laser_module.get_scan_xy_offset(scan_config=scan_config, side=side),
			should_abort=None,
		)

	result = await laser_module.run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		didRun=didRunProbe,
		shutdown_action=args.shutdown_action,
	)

	return report_result(
		result=result,
		manual_reference=manual_reference,
		scan_config=scan_config,
		side=side,
	)


if __name__ == "__main__":
	raise SystemExit(asyncio.run(main()))
