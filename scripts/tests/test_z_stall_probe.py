from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest
from pathlib import Path
from types import ModuleType, SimpleNamespace

import openshelf_os.laser_calibration as laser_module
from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
)
from typing import Any


PROBE_PATH = Path(__file__).parents[1] / "z_stall_probe.py"


def load_probe_module() -> ModuleType:
	spec = importlib.util.spec_from_file_location("z_stall_probe_test", PROBE_PATH)
	assert spec is not None
	assert spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def build_config(config_path: Path, storage_path: Path) -> SimpleNamespace:
	scan = SimpleNamespace(
		z_stall_probe_x_shift_mm=20.0,
		z_stall_manual_agreement_mm=1.0,
		z_offset_model_param_tolerance_mm=0.2,
		z_stall_probe_repeats=2,
		z_stall_probe_repeat_tolerance_mm=0.3,
		left_xy_offset=SimpleNamespace(x_mm=-1.25, y_mm=-2.75),
		right_xy_offset=None,
	)
	tool = SimpleNamespace(
		motors=[SimpleNamespace(motor_name="x_axis", lim=950.0, lower_lim=0.0)],
	)
	config = SimpleNamespace(
		robot=SimpleNamespace(
			grid=SimpleNamespace(scan=scan, path=str(storage_path)),
			tool=tool,
		),
		storage=SimpleNamespace(bins=[]),
	)
	config.robot.grid.scan.model_copy = lambda update=None: SimpleNamespace(**{**vars(scan), **(update or {})})

	return config


def build_grid() -> StorageGrid:
	grid = StorageGrid(filename="storage.json")
	grid.left_module = [
		Shelf(
			shelf_idx=1,
			shelf_height=30.0,
			shelf_width=400.0,
			cells=[
				Cell(
					cell_idx=0,
					status=CellStatus.AVAILABLE,
					item=None,
					calibration_point=CalibrationCoordinate(x=341.75, y=239.5, s=0, z_offset=6.5),
					dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
				),
				Cell(
					cell_idx=1,
					status=CellStatus.AVAILABLE,
					item=None,
					calibration_point=CalibrationCoordinate(
						x=476.75,
						y=239.5,
						s=0,
						z_offset=6.5,
						z_captured_at_stage_mm=47.0,
					),
					dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
					is_manual=True,
				),
			],
		)
	]
	return grid


def build_run_dir(run_dir: Path) -> Path:
	run_dir.mkdir(parents=True, exist_ok=True)
	metadata = {
		"fine_scans": [
			{
				"detected_cells": [
					{
						"shelf_idx": 1,
						"cell_idx": 0,
						"center_x_mm": 290.75,
						"center_y_mm": 239.5,
						"tool_x_mm": 341.75,
						"tool_y_mm": 239.5,
						"width_mm": 26.0,
						"height_mm": 30.0,
						"point_count": 20,
					},
					{
						"shelf_idx": 1,
						"cell_idx": 1,
						"center_x_mm": 425.75,
						"center_y_mm": 239.5,
						"tool_x_mm": 476.75,
						"tool_y_mm": 239.5,
						"width_mm": 26.0,
						"height_mm": 30.0,
						"point_count": 20,
					},
				]
			}
		],
		"candidates": [
			{
				"shelf_idx": 1,
				"x_min": 277.0,
				"x_max": 303.0,
				"y_min": 231.25,
				"y_max": 231.25,
				"row_count": 1,
				"wall_depth_mm": 9.58,
				"wall_sample_count": 4,
			},
			{
				"shelf_idx": 1,
				"x_min": 412.0,
				"x_max": 438.0,
				"y_min": 231.25,
				"y_max": 231.25,
				"row_count": 1,
				"wall_depth_mm": 9.62,
				"wall_sample_count": 4,
			}
		],
	}
	(run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
	return run_dir


def test_plan_only_reports_probes_and_the_reference_datum(tmp_path: Path, monkeypatch, capsys) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "3bf2cfc1")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe, ["--module", "left", "--from-run", str(run_dir), "--plan-only"]
	)

	assert exit_code == 0
	output = capsys.readouterr().out
	# Only the cell whose sweep candidate carried a wall depth is planned: the probe subtracts it.
	assert "probe candidates (1):" in output
	# Run-frame detection, so the probe lands at tool_x + xy_offset + shift = 341.75 - 1.25 + 20.
	assert "1:0 probe_x=360.50" in output
	assert "hole tool_x=341.75" in output
	assert "reference datum: 1 cell(s) imply intercept -3.120mm" in output
	assert "captured_stage_mm=47.00" in output


def test_shelf_count_override_decides_how_many_shelves_are_sampled(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "3bf2cfc1")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe,
		["--module", "left", "--from-run", str(run_dir), "--plan-only", "--shelves", "1"],
	)

	assert exit_code == 0
	output = capsys.readouterr().out
	assert "one opening on each of 1 shelf(s) spread over the module" in output


def main_with_args(probe: ModuleType, argv: list[str]) -> int:
	"""Run main() with argv, since argparse reads sys.argv directly."""
	original_argv = sys.argv
	sys.argv = ["z_stall_probe.py", *argv]

	try:
		return asyncio.run(probe.main())
	finally:
		sys.argv = original_argv


def test_plan_only_still_plans_without_a_reference_datum(tmp_path: Path, monkeypatch, capsys) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	# Nothing is marked manual: the probe is still worth running, it just has no verdict.
	for shelf in grid.left_module:
		for cell in shelf.cells:
			cell.is_manual = False
	run_dir = build_run_dir(tmp_path / "runs" / "4fab1bf8")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe, ["--module", "left", "--from-run", str(run_dir), "--plan-only"]
	)

	assert exit_code == 0
	output = capsys.readouterr().out
	assert "probe candidates (1):" in output
	assert "reference datum unavailable" in output or "reference datum: none" in output


def test_side_mismatch_is_refused_and_default_picks_the_matching_run(tmp_path: Path, monkeypatch, capsys) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	left_run = build_run_dir(tmp_path / "runs" / "left")
	right_run = build_run_dir(tmp_path / "runs" / "right")
	right_metadata = json.loads((right_run / "metadata.json").read_text(encoding="utf-8"))
	right_metadata["side"] = "right_module"
	(right_run / "metadata.json").write_text(json.dumps(right_metadata), encoding="utf-8")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	with pytest.raises(ValueError, match="holds a right_module run"):
		main_with_args(probe, ["--module", "left", "--from-run", str(right_run), "--plan-only"])

	# Both metadata files lack a side otherwise, so mark the left one and let the default pick it.
	left_metadata = json.loads((left_run / "metadata.json").read_text(encoding="utf-8"))
	left_metadata["side"] = "left_module"
	(left_run / "metadata.json").write_text(json.dumps(left_metadata), encoding="utf-8")
	monkeypatch.setattr(probe, "paths_for_config_file", lambda path: SimpleNamespace(calibration_dir=tmp_path / "runs"))

	exit_code = main_with_args(probe, ["--module", "left", "--plan-only"])

	assert exit_code == 0
	output = capsys.readouterr().out
	assert str(left_run) in output


def test_explicit_exclusions_shield_a_cell_from_the_probe(tmp_path: Path, monkeypatch, capsys) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")
	metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
	metadata["side"] = "left_module"
	(run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe,
		[
			"--module",
			"left",
			"--from-run",
			str(run_dir),
			"--plan-only",
			"--exclude",
			"1:0",
		],
	)

	output = capsys.readouterr().out
	assert "excluded 2 cell(s) from probing: 1:0, 1:1" in output  # 1:0 by --exclude, 1:1 is the datum
	# Shielding the cell the probe would have used leaves nothing to press, so this is a refusal
	# rather than an empty plan - a probe run that probes nothing proves nothing.
	assert exit_code == 1
	assert "nothing to probe" in output


def test_from_grid_probes_the_stored_positions_with_the_sweep_depths(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")
	metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
	metadata["side"] = "left_module"
	(run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe,
		["--module", "left", "--from-run", str(run_dir), "--from-grid", "--shelf", "1", "--plan-only"],
	)

	assert exit_code == 0
	output = capsys.readouterr().out
	assert "positions: stored calibration points (2 sweep wall depths available)" in output
	# Grid positions are converted back out of the grab frame, so the probe lands the divider shift
	# away from the verified point: 341.75 + 20 = 361.75, with the xy offset cancelling.
	assert "1:0 probe_x=361.75" in output
	assert "hole tool_x=343.00" in output


def build_fake_anchor_result(
	*,
	intercept_mm: float = -3.12,
	slope_per_mm: float = 0.0,
	stage_extension_mm: float = 47.0,
	fit_centroid_x_mm: float = 300.0,
) -> SimpleNamespace:
	# Matches the datum the test grid implies (intercept -3.120mm), so the verdict is in tolerance.
	return SimpleNamespace(
		fit_source="test",
		sample_count=1,
		x_span_mm=135.0,
		intercept_mm=intercept_mm,
		slope_per_mm=slope_per_mm,
		stage_extension_mm=stage_extension_mm,
		z_place_mm=10.0,
		rotation_center_deg=3.0,
		fit_centroid_x_mm=fit_centroid_x_mm,
		samples=(),
		dropped_repeats=(),
		skipped=(),
	)


def test_run_hands_the_anchor_the_raw_axis_limits(tmp_path: Path, monkeypatch, capsys) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	captured: dict[str, Any] = {}

	async def fake_run_z_stall_anchor(**kwargs: Any) -> Any:
		captured.update(kwargs)
		return build_fake_anchor_result()

	async def fake_run_with_temp_robot_config(*, config, config_path, didRun, shutdown_action) -> Any:
		return await didRun(SimpleNamespace(_tool=object()))

	monkeypatch.setattr(laser_module, "run_z_stall_anchor", fake_run_z_stall_anchor)
	monkeypatch.setattr(laser_module, "run_with_temp_robot_config", fake_run_with_temp_robot_config)

	exit_code = main_with_args(probe, ["--module", "left", "--from-run", str(run_dir)])

	assert exit_code == 0
	# The anchor subtracts the xy offset and the probe shift itself, so the script must hand it the
	# raw axis limits. A pre-converted limit makes the anchor subtract the offset a second time,
	# which loosens the reachability guard by the offset.
	x_axis_motor = config.robot.tool.motors[0]
	offset_x_mm = float(config.robot.grid.scan.left_xy_offset.x_mm)
	assert captured["x_axis_limit_mm"] == float(x_axis_motor.lim)
	assert captured["x_axis_lower_limit_mm"] == float(x_axis_motor.lower_lim)
	converted = (
		float(x_axis_motor.lim)
		- offset_x_mm
		- float(config.robot.grid.scan.z_stall_probe_x_shift_mm)
	)
	assert captured["x_axis_limit_mm"] != converted


def test_run_hands_the_anchor_the_requested_shelf_count(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	captured: dict[str, Any] = {}

	async def fake_run_z_stall_anchor(**kwargs: Any) -> Any:
		captured.update(kwargs)
		return build_fake_anchor_result()

	async def fake_run_with_temp_robot_config(*, config, config_path, didRun, shutdown_action) -> Any:
		return await didRun(SimpleNamespace(_tool=object()))

	monkeypatch.setattr(laser_module, "run_z_stall_anchor", fake_run_z_stall_anchor)
	monkeypatch.setattr(laser_module, "run_with_temp_robot_config", fake_run_with_temp_robot_config)

	exit_code = main_with_args(
		probe, ["--module", "left", "--from-run", str(run_dir), "--shelves", "4"]
	)

	assert exit_code == 0
	# The anchor re-selects its probes from z_stall_probe_shelves, so the flag must reach the
	# scan_config the run uses: otherwise --shelves 4 validates a 4-shelf plan and then probes
	# the configured default.
	assert captured["scan_config"].z_stall_probe_shelves == 4


def test_probe_slope_the_datum_cannot_corroborate_fails_the_verdict(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	# Same intercept, tilted fit: the bare intercept difference is 0, but where the two models
	# put the datum cell they differ by (S - S_cfg) * x, which the product gate refuses.
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	async def fake_run_z_stall_anchor(**kwargs: Any) -> Any:
		return build_fake_anchor_result(slope_per_mm=0.005)

	async def fake_run_with_temp_robot_config(*, config, config_path, didRun, shutdown_action) -> Any:
		return await didRun(SimpleNamespace(_tool=object()))

	monkeypatch.setattr(laser_module, "run_z_stall_anchor", fake_run_z_stall_anchor)
	monkeypatch.setattr(laser_module, "run_with_temp_robot_config", fake_run_with_temp_robot_config)

	exit_code = main_with_args(probe, ["--module", "left", "--from-run", str(run_dir)])

	assert exit_code == 1
	output = capsys.readouterr().out
	# The tilt differs from the datum by 0.005 * 476.75mm at the manual cell's grab x.
	assert "2.384mm" in output
	assert "OUT OF TOLERANCE" in output


def test_from_grid_pairs_run_depths_with_stored_cells_by_position(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	"""The run's depths are keyed in its own analysis frame, the probe targets in the grid's.

	A partial run re-indexes its shelf to 0 and renumbers the openings it saw, so keying the
	depths by grid index would attach the wrong depth - or none at all.
	"""
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")
	metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
	metadata["side"] = "left_module"
	# The run saw one opening, as a partial run does, and re-indexed it to shelf 0.
	metadata["fine_scans"] = [
		{
			"detected_cells": [
				{
					"shelf_idx": 0,
					"cell_idx": 0,
					"center_x_mm": 290.75,
					"center_y_mm": 239.5,
					"tool_x_mm": 341.75,
					"tool_y_mm": 239.5,
					"width_mm": 26.0,
					"height_mm": 30.0,
					"point_count": 20,
				}
			]
		}
	]
	metadata["candidates"] = [
		{
			"shelf_idx": 0,
			"x_min": 277.0,
			"x_max": 303.0,
			"y_min": 231.25,
			"y_max": 231.25,
			"row_count": 1,
			"wall_depth_mm": 9.42,
			"wall_sample_count": 4,
		}
	]
	(run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	exit_code = main_with_args(
		probe, ["--module", "left", "--from-run", str(run_dir), "--from-grid", "--plan-only"]
	)

	assert exit_code == 0
	output = capsys.readouterr().out
	# The stored cell 1:0 (x 341.75) is the opening the run saw: it gets that run's depth, 9.42mm.
	assert "probe candidates (1):" in output
	assert "1:0 probe_x=" in output
	assert "wall_depth_mm=9.420" in output


def test_short_span_probe_reports_the_model_the_product_would_install(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	# A tilt-free fit reports the deltas' value at the fit centre, so keeping the machine's tilt
	# means re-basing the intercept: the script must print that pair, not the raw fit.
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	config.robot.grid.scan.left_z_offset_model = SimpleNamespace(
		intercept_mm=-4.0735,
		slope_per_mm=0.002,
		stage_extension_mm=47.0,
		z_place_mm=10.0,
	)
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	# The fixture's datum cell (1:1, x 476.75, z 6.5, wall 9.62) implies this intercept under the
	# configured tilt; a probe that measures the same line reports it at the fit centre instead.
	datum_intercept_mm = 6.5 - 9.62 - 0.002 * 476.75

	async def fake_run_z_stall_anchor(**kwargs: Any) -> Any:
		# Span below Z_DELTA_TILT_MIN_X_SPAN_MM, so no tilt is fitted and the datum is kept.
		return build_fake_anchor_result(
			slope_per_mm=0.0,
			intercept_mm=datum_intercept_mm + 0.002 * 300.0,
			fit_centroid_x_mm=300.0,
		)

	async def fake_run_with_temp_robot_config(*, config, config_path, didRun, shutdown_action) -> Any:
		return await didRun(SimpleNamespace(_tool=object()))

	monkeypatch.setattr(laser_module, "run_z_stall_anchor", fake_run_z_stall_anchor)
	monkeypatch.setattr(laser_module, "run_with_temp_robot_config", fake_run_with_temp_robot_config)

	exit_code = main_with_args(probe, ["--module", "left", "--from-run", str(run_dir)])

	output = capsys.readouterr().out

	assert exit_code == 0
	# The installed pair is the datum's line: intercept re-based to x = 0, slope kept.
	assert f"'intercept_mm': {round(datum_intercept_mm, 3)}" in output
	assert "'slope_per_mm': 0.002" in output
	assert "model_source=config_short_span" in output
	# The raw fit is still reported, so the substitution is visible.
	assert f"raw_probe_intercept_mm={datum_intercept_mm + 0.002 * 300.0:.3f}" in output


def test_datum_saved_at_another_stage_extension_fails_the_verdict(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	# The fit agrees with the datum, but its z was captured with the stage elsewhere: the
	# datum's z is not comparable, and the product refuses the model for exactly that.
	probe = load_probe_module()
	config_path = tmp_path / "config.development.json"
	config_path.write_text("{}", encoding="utf-8")
	config = build_config(config_path, tmp_path / "storage.development.json")
	grid = build_grid()
	run_dir = build_run_dir(tmp_path / "runs" / "left")

	monkeypatch.setattr(probe, "load_config_file", lambda path: config)
	monkeypatch.setattr(laser_module, "load_grid", lambda path: grid)
	monkeypatch.setattr(
		probe.RuntimeStatePaths,
		"current",
		classmethod(lambda cls: SimpleNamespace(resolve_storage_path=lambda *a, **k: str(tmp_path / "data" / "storage.json"))),
	)

	async def fake_run_z_stall_anchor(**kwargs: Any) -> Any:
		return build_fake_anchor_result(stage_extension_mm=48.0)

	async def fake_run_with_temp_robot_config(*, config, config_path, didRun, shutdown_action) -> Any:
		return await didRun(SimpleNamespace(_tool=object()))

	monkeypatch.setattr(laser_module, "run_z_stall_anchor", fake_run_z_stall_anchor)
	monkeypatch.setattr(laser_module, "run_with_temp_robot_config", fake_run_with_temp_robot_config)

	exit_code = main_with_args(probe, ["--module", "left", "--from-run", str(run_dir)])

	assert exit_code == 1
	output = capsys.readouterr().out
	# The fit is within tolerance: the stage pin is what refuses it.
	assert "within tolerance" in output
	assert "NOT comparable" in output
