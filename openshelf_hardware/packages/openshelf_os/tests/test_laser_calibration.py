import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openshelf_os.laser_calibration import (
	require_z_offset_model_matches_z_place,
	z_offset_model_intercept_at_stage,
	apply_wall_depth_z_offsets,
	apply_xy_reference_offsets,
	build_shelves_from_scan,
	enter_scan_mode,
	find_gap_segments_in_line,
	disable_unreachable_cells,
	fit_z_delta_line,
	is_row_refinement_settled,
	load_staged_scan_analysis,
	resolve_z_delta_model,
	run_staged_scan,
	snap_shelf_cells_to_y_line,
	BACK_MODULE_ROTATION_SAFE_X_MM,
)
from openshelf_os.laser_calibration.angle.robot_ops import probe_z_until_stall
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageModuleSide


def write_flying_replay_run(tmp_path: Path) -> Path:
	"""A 1mm-grid run whose recorded cell carries edges a flying pass interpolated."""
	np = pytest.importorskip("numpy")
	run_dir = tmp_path / "flying-run"
	run_dir.mkdir()
	step_mm = 1.0
	x_min, x_max = 8.2, 34.2
	y_min, y_max = 5.2, 35.2
	positions = [float(value) for value in range(0, 41)]
	fine_depth = np.full((len(positions), len(positions)), 10.0, dtype=np.float32)

	for row_idx, y_mm in enumerate(positions):
		for col_idx, x_mm in enumerate(positions):
			if x_min < x_mm < x_max and y_min < y_mm < y_max:
				fine_depth[row_idx, col_idx] = 0.0

	fine_path = run_dir / "fine_00.npy"
	np.save(fine_path, fine_depth)
	metadata = {
		"run_id": "flying-run",
		"status": "completed",
		"side": StorageModuleSide.FRONT.value,
		"bounds": {"x_min": 0.0, "x_max": 40.0, "y_min": 0.0, "y_max": 40.0},
		"fine_step_mm": step_mm,
		"coarse_step_mm": 4.0,
		"samples_per_point": 1,
		"settle_ms": 0,
		"wall_depth_mm": 10.0,
		"coarse_scan": {"raw_scan_path": "coarse_depth.npy"},
		"candidates": [
			{
				"x_min": 8.0,
				"x_max": 34.0,
				"y_min": 5.0,
				"y_max": 35.0,
				"row_count": 1,
				"wall_depth_mm": 10.0,
				"wall_sample_count": 1,
			}
		],
		"fine_scans": [
			{
				"candidate_index": 0,
				"status": "completed",
				"bounds": {"x_min": 0.0, "x_max": 40.0, "y_min": 0.0, "y_max": 40.0},
				"raw_scan_path": str(fine_path),
				"detected_cells": [
					{
						"shelf_idx": 0,
						"cell_idx": 0,
						"center_x_mm": (x_min + x_max) / 2.0,
						"center_y_mm": (y_min + y_max) / 2.0,
						"tool_x_mm": (x_min + x_max) / 2.0 + 1.0,
						"tool_y_mm": (y_min + y_max) / 2.0 - 2.0,
						"width_mm": x_max - x_min,
						"height_mm": y_max - y_min,
						"point_count": 26,
						"bbox_x_min": x_min + (step_mm / 2.0),
						"bbox_x_max": x_max - (step_mm / 2.0),
						"bbox_y_min": y_min + (step_mm / 2.0),
						"bbox_y_max": y_max - (step_mm / 2.0),
					}
				],
			}
		],
	}
	(run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
	return run_dir


def test_load_staged_scan_analysis_keeps_the_measured_flying_cell_geometry(
	tmp_path: Path,
) -> None:
	run_dir = write_flying_replay_run(tmp_path)

	analysis = load_staged_scan_analysis(
		run_dir,
		# Only the per-machine laser offsets reach a replay from the scan config.
		scan_config=SimpleNamespace(laser_offset_x_mm=1.0, laser_offset_y_mm=2.0),
	)

	assert len(analysis.shelves) == 1
	assert len(analysis.shelves[0].cells) == 1
	cell = analysis.shelves[0].cells[0]

	# The grid quantizes the 1mm rows to 21.5/20.5; the flying pass measured 21.2/20.2.
	assert cell.center_x_mm == pytest.approx(21.2)
	assert cell.center_y_mm == pytest.approx(20.2)
	assert cell.width_mm == pytest.approx(26.0)
	assert cell.height_mm == pytest.approx(30.0)
	# Tool coordinates still come from the current config's laser offsets.
	assert cell.tool_x_mm == pytest.approx(21.2 + 1.0)
	assert cell.tool_y_mm == pytest.approx(20.2 - 2.0)


def test_find_gap_segments_in_line_matches_expected_hole_width() -> None:
	positions = [float(idx * 4) for idx in range(12)]
	values = [10.0, 10.0, 10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
	scan_config = SimpleNamespace(
	)

	wall_depth_mm, segments = find_gap_segments_in_line(
		values=values,
		positions=positions,
		step_mm=4.0,
		scan_config=scan_config,
	)

	assert wall_depth_mm == pytest.approx(10.0)
	assert len(segments) == 1
	assert segments[0].width_mm == pytest.approx(24.0)


@pytest.mark.asyncio
async def test_run_staged_scan_skips_retract_when_cancelled(tmp_path: Path) -> None:
	run_dir = tmp_path / 'scan-run'
	run_dir.mkdir()
	tool = SimpleNamespace(retract=AsyncMock())
	robot = SimpleNamespace(_tool=tool)
	recorder = SimpleNamespace(
		run_id='run-id',
		run_dir=run_dir,
		log_path=run_dir / 'scan.log',
		metadata_path=run_dir / 'metadata.json',
		coarse_depth_path=run_dir / 'coarse_depth.npy',
	)
	started = asyncio.Event()

	async def didCaptureShelfCandidates(*args, **kwargs):
		started.set()
		await asyncio.sleep(10)

	with patch('openshelf_os.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('openshelf_os.laser_calibration.capture_shelf_candidates', side_effect=didCaptureShelfCandidates):
			with patch('openshelf_os.laser_calibration.persist_staged_metadata'):
				with patch('openshelf_os.laser_calibration.persist_scan_started_at'):
					task = asyncio.create_task(
						run_staged_scan(
							robot=robot,
							side=StorageModuleSide.FRONT,
							bounds=ScanBounds(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
							fine_step_mm=1.0,
							settle_ms=0,
							samples_per_point=1,
							coarse_settle_ms=0,
							coarse_samples_per_point=1,
							scan_config=SimpleNamespace(
								laser_offset_x_mm=1.0,
								laser_offset_y_mm=0.0,
								front_gripper_xy_correction_mm=SimpleNamespace(x_mm=0.0, y_mm=0.0),
							),
							scans_dir=str(tmp_path),
							run_parameters={},
							shelf_scout_only=False,
							known_shelf_edge_y=None,
							recorder=recorder,
						)
					)

					await started.wait()
					task.cancel()

					with pytest.raises(asyncio.CancelledError):
						await task

	tool.retract.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_staged_scan_raises_when_abort_requested_after_coarse_scan(tmp_path: Path) -> None:
	run_dir = tmp_path / 'scan-run'
	run_dir.mkdir()
	tool = SimpleNamespace(retract=AsyncMock())
	robot = SimpleNamespace(_tool=tool)
	recorder = SimpleNamespace(
		run_id='run-id',
		run_dir=run_dir,
		log_path=run_dir / 'scan.log',
		metadata_path=run_dir / 'metadata.json',
		coarse_depth_path=run_dir / 'coarse_depth.npy',
	)
	should_abort = False

	async def didCaptureShelfCandidates(*args, **kwargs):
		nonlocal should_abort
		should_abort = True
		return SimpleNamespace(depth_rows=[]), 1.0, [], []

	with patch('openshelf_os.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('openshelf_os.laser_calibration.capture_shelf_candidates', side_effect=didCaptureShelfCandidates):
			with patch('openshelf_os.laser_calibration.persist_staged_metadata'):
				with patch('openshelf_os.laser_calibration.persist_scan_started_at'):
					with pytest.raises(asyncio.CancelledError):
						await run_staged_scan(
							robot=robot,
							side=StorageModuleSide.FRONT,
							bounds=ScanBounds(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
							fine_step_mm=1.0,
							settle_ms=0,
							samples_per_point=1,
							coarse_settle_ms=0,
							coarse_samples_per_point=1,
							scan_config=SimpleNamespace(
								laser_offset_x_mm=1.0,
								laser_offset_y_mm=0.0,
								front_gripper_xy_correction_mm=SimpleNamespace(x_mm=0.0, y_mm=0.0),
							),
							scans_dir=str(tmp_path),
							run_parameters={},
							shelf_scout_only=False,
							known_shelf_edge_y=None,
							recorder=recorder,
							should_abort=lambda: should_abort,
						)

	tool.retract.assert_not_awaited()


@pytest.mark.asyncio
async def test_enter_scan_mode_uses_safe_x_for_back_module() -> None:
	params = {
		"SCAN_POSITION": [900.0, 1200.0],
		"Z_EXTENSION_PLACE": 100.0,
		"Z_MOVE_RPM": 50.0,
		"Z_MOVE_ACCEL": 60.0,
		"ROTATION_FRONT": 0.0,
		"ROTATION_BACK": 180.0,
	}
	tool = SimpleNamespace(
		_laser_enabled=True,
		_laser=object(),
		_mx=object(),
		_my=object(),
		_mr=SimpleNamespace(get_position=AsyncMock(return_value=180.0)),
		_ms=SimpleNamespace(ready=AsyncMock(), move_to_position=AsyncMock()),
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
		get_param=AsyncMock(side_effect=lambda name: params[name]),
		retract=AsyncMock(),
		move=AsyncMock(),
		rotate_to_position=AsyncMock(),
	)

	await enter_scan_mode(
		tool,
		side=StorageModuleSide.BACK,
		scan_config=SimpleNamespace(),
	)

	first_move = tool.move.await_args_list[0].args[0]
	second_move = tool.move.await_args_list[1].args[0]
	assert first_move.x == pytest.approx(BACK_MODULE_ROTATION_SAFE_X_MM)
	assert first_move.y == pytest.approx(1200.0)
	assert first_move.s == 1
	assert second_move.x == pytest.approx(900.0)
	assert second_move.y == pytest.approx(1200.0)
	assert second_move.s == 1


def test_apply_xy_reference_offsets_requires_fixed_offset() -> None:
	with pytest.raises(ValueError, match='robot.grid.scan.front_gripper_xy_correction_mm must be configured'):
		apply_xy_reference_offsets(
			scanned_shelves=[],
			scan_config=SimpleNamespace(front_gripper_xy_correction_mm=None),
			side=StorageModuleSide.FRONT,
		)


def _detected_scan_shelf(shelf_idx: int, tool_xs: list[float]) -> SimpleNamespace:
	return SimpleNamespace(
		shelf_idx=shelf_idx,
		shelf_width_mm=200.0,
		cells=[
			SimpleNamespace(
				shelf_idx=shelf_idx,
				cell_idx=cell_idx,
				tool_x_mm=tool_x,
				tool_y_mm=100.0,
				width_mm=26.0,
				height_mm=30.0,
			)
			for cell_idx, tool_x in enumerate(tool_xs)
		],
	)


def _existing_calibrated_shelf(shelf_idx: int, xs: list[float]) -> Shelf:
	return Shelf(
		shelf_idx=shelf_idx,
		divider_width=13.0,
		cells=[
			Cell(
				cell_idx=cell_idx,
				status=CellStatus.AVAILABLE,
				item=None,
				calibration_point=CalibrationCoordinate(x=x, y=20.0, z_offset=float(cell_idx), s=0),
				dimensions=Dimensions(width=26.0, height=30.0, length=400.0),
			)
			for cell_idx, x in enumerate(xs)
		],
	)


def test_build_shelves_from_scan_preserves_dropped_cell_index() -> None:
	analysis = SimpleNamespace(shelves=[_detected_scan_shelf(0, [493.0, 659.0, 823.0])])
	existing = [_existing_calibrated_shelf(0, [329.0, 494.0, 660.0, 824.0])]
	detected_cell_key_map: dict[tuple[int, int], tuple[int, int]] = {}

	result = build_shelves_from_scan(
		side=StorageModuleSide.FRONT,
		analysis=analysis,
		existing_shelves=existing,
		detected_cell_key_map=detected_cell_key_map,
	)

	cells = sorted(result[0].cells, key=lambda cell: cell.cell_idx)
	assert [cell.cell_idx for cell in cells] == [0, 1, 2, 3]
	assert detected_cell_key_map == {
		(0, 0): (0, 1),
		(0, 1): (0, 2),
		(0, 2): (0, 3),
	}
	assert cells[0].calibration_point.x == pytest.approx(329.0)
	assert cells[0].calibration_point.z_offset == pytest.approx(0.0)
	assert cells[1].calibration_point.x == pytest.approx(493.0)
	assert cells[2].calibration_point.x == pytest.approx(659.0)
	assert cells[3].calibration_point.x == pytest.approx(823.0)


def test_fit_z_delta_line_two_points_reproduces_validated_ramp() -> None:
	# Right-side hand-validated ramp: +2 mm of extra Z across an 824 mm X span.
	intercept_mm, slope_per_mm = fit_z_delta_line([(36.0, 4.0), (860.0, 6.0)])

	assert slope_per_mm == pytest.approx(2.0 / 824.0)
	assert intercept_mm + slope_per_mm * 36.0 == pytest.approx(4.0)
	assert intercept_mm + slope_per_mm * 860.0 == pytest.approx(6.0)
	assert intercept_mm + slope_per_mm * 448.0 == pytest.approx(5.0)


def test_fit_z_delta_line_short_x_span_collapses_to_constant() -> None:
	intercept_mm, slope_per_mm = fit_z_delta_line([(400.0, 3.0), (500.0, 5.0)])

	assert slope_per_mm == 0.0
	assert intercept_mm == pytest.approx(4.0)


def test_fit_z_delta_line_implausible_tilt_collapses_to_constant() -> None:
	# 5 mm of tilt across an 824 mm span exceeds the max plausible rack tilt.
	intercept_mm, slope_per_mm = fit_z_delta_line([(36.0, 3.0), (860.0, 8.0)])

	assert slope_per_mm == 0.0
	assert intercept_mm == pytest.approx(5.5)


def test_resolve_z_delta_model_corroborated_tilt_is_kept() -> None:
	# Extremes imply a +2mm/824mm tilt; the mid probe lands on the line -> tilt kept.
	intercept_mm, slope_per_mm = resolve_z_delta_model([(36.0, 4.0), (860.0, 6.0), (450.0, 5.0)])

	assert slope_per_mm == pytest.approx(2.0 / 824.0)
	assert intercept_mm + slope_per_mm * 36.0 == pytest.approx(4.0)


def test_resolve_z_delta_model_uncorroborated_tilt_collapses_to_median_constant() -> None:
	# The x=850 extreme stalled shallow (0.32) implying a steep tilt, but the mid probe
	# (1.70) disagrees with that line by >0.5mm -> reject tilt, use the median delta.
	intercept_mm, slope_per_mm = resolve_z_delta_model([(310.0, 1.77), (850.0, 0.32), (580.0, 1.70)])

	assert slope_per_mm == 0.0
	assert intercept_mm == pytest.approx(1.70)


def _reachability_cell(cell_idx: int, x: float) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=CalibrationCoordinate(x=x, y=995.25, z_offset=13.3, s=0),
		dimensions=Dimensions(width=40.0, height=50.0, length=60.0),
	)


def test_disable_unreachable_cells_disables_only_cells_past_the_x_axis_limit() -> None:
	# Real lb4-3 shelf: the laser sits ahead of the tool, so the last cell's grab X (956)
	# lands past the 950mm X-axis travel limit and the tool can never reach it.
	shelf = Shelf(
		shelf_idx=3,
		divider_width=13.0,
		cells=[
			_reachability_cell(4, 746.0),
			_reachability_cell(5, 851.0),
			_reachability_cell(6, 956.0),
		],
	)

	disabled_count = disable_unreachable_cells(scanned_shelves=[shelf], x_axis_limit_mm=950.0)

	assert disabled_count == 1
	assert shelf.cells[0].status == CellStatus.AVAILABLE
	assert shelf.cells[1].status == CellStatus.AVAILABLE
	assert shelf.cells[2].status == CellStatus.DISABLED


def test_snap_shelf_cells_to_y_line_snaps_only_outliers() -> None:
	# Clean cells lie on y = 100 + 0.01*x; one cell (x=300) is a +7 mm detection spike.
	cells = [
		SimpleNamespace(center_x_mm=0.0, center_y_mm=100.0, tool_y_mm=50.0),
		SimpleNamespace(center_x_mm=100.0, center_y_mm=101.0, tool_y_mm=51.0),
		SimpleNamespace(center_x_mm=200.0, center_y_mm=102.0, tool_y_mm=52.0),
		SimpleNamespace(center_x_mm=300.0, center_y_mm=110.0, tool_y_mm=60.0),
		SimpleNamespace(center_x_mm=400.0, center_y_mm=104.0, tool_y_mm=54.0),
	]

	snap_shelf_cells_to_y_line(cells)

	assert cells[0].center_y_mm == pytest.approx(100.0)
	assert cells[1].center_y_mm == pytest.approx(101.0)
	assert cells[4].center_y_mm == pytest.approx(104.0)
	assert cells[3].center_y_mm == pytest.approx(103.0, abs=0.5)
	# tool_y shifts by the same correction that moved center_y (110 -> 103 => -7).
	assert cells[3].tool_y_mm == pytest.approx(53.0, abs=0.5)


def _row_segment(center_mm: float) -> object:
	return SimpleNamespace(center_mm=center_mm, width_mm=26.0, start_mm=center_mm - 13.0, end_mm=center_mm + 13.0)


def test_row_refinement_settles_when_three_rows_agree() -> None:
	segments = [_row_segment(500.0), _row_segment(500.2), _row_segment(500.4)]

	assert is_row_refinement_settled(segments) is True


def _build_probe_tool(fast_move_error: Exception | None = None) -> SimpleNamespace:
	positions = {'z': 0.0}

	async def move_to_position(target_mm: float, rpm: int | None = None) -> None:
		if fast_move_error is not None and target_mm > 100.0:
			positions['z'] = 56.2
			raise fast_move_error

		positions['z'] = float(target_mm)

	async def get_position() -> float:
		return positions['z']

	motor = SimpleNamespace(
		move_to_position=move_to_position,
		move_to_position_no_wait=AsyncMock(),
		get_position=get_position,
		get_param=AsyncMock(return_value=0),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
	)
	return SimpleNamespace(_mg=motor)


def test_probe_z_until_stall_does_not_report_fast_approach_fault_as_wall_contact() -> None:
	tool = _build_probe_tool(fast_move_error=RuntimeError('Movement failed for motor 30 (status=FAULT)'))

	did_stall, z_contact_mm, reason, _alarm = asyncio.run(
		probe_z_until_stall(
			tool,
			target_z_mm=180.0,
			retract_z_mm=5.0,
			settle_ms=0,
			timeout_s=5.0,
			probe_rpm=20,
			no_progress_s=0.25,
			probe_step_mm=0.5,
			fast_approach_mm=130.0,
			fast_approach_rpm=20,
			auto_retract=True,
		)
	)

	assert reason == 'fast_approach_fault'
	assert did_stall is False
	assert z_contact_mm == pytest.approx(56.2)


def test_z_offset_model_is_carried_to_the_live_stage_extension() -> None:
	# The z axis rides on the stage: one millimetre less stage needs one millimetre more z.
	model = SimpleNamespace(intercept_mm=1.052, x_slope_mm_per_mm=-4.5e-05, reference_stage_mm=47.0, reference_z_place_mm=160.5)

	assert z_offset_model_intercept_at_stage(model=model, stage_extension_mm=46.0) == pytest.approx(2.052)
	assert z_offset_model_intercept_at_stage(model=model, stage_extension_mm=48.0) == pytest.approx(0.052)


def test_require_z_offset_model_matches_z_place_rejects_a_moved_place_height() -> None:
	model = SimpleNamespace(intercept_mm=4.85, x_slope_mm_per_mm=-0.00285, reference_stage_mm=46.0, reference_z_place_mm=160.5)

	require_z_offset_model_matches_z_place(model=model, side=StorageModuleSide.FRONT, z_place_mm=160.6, tolerance_mm=0.2)

	with pytest.raises(ValueError, match='Z_EXTENSION_PLACE 160.50mm'):
		require_z_offset_model_matches_z_place(
			model=model,
			side=StorageModuleSide.FRONT,
			z_place_mm=162.0,
			tolerance_mm=0.2,
		)


def test_apply_wall_depth_z_offsets_refuses_cells_without_wall_depth(tmp_path: Path) -> None:
	# A cell with no laser wall depth used to be skipped silently, leaving it at the
	# z_offset=0.0 default -- a blank cell in an otherwise applied grid.
	metadata_path = tmp_path / 'metadata.json'
	metadata_path.write_text(
		json.dumps(
			{
				'candidates': [
					{'x_min': 279.0, 'x_max': 307.0, 'y_min': 476.0, 'y_max': 504.0, 'wall_depth_mm': 9.9},
				],
				'fine_scans': [
					{
						'candidate_index': 0,
						'status': 'completed',
						'detected_cells': [
							{'shelf_idx': 0, 'cell_idx': 0, 'center_x_mm': 293.0, 'center_y_mm': 490.0},
						],
					}
				],
			}
		),
		encoding='utf-8',
	)
	analysis = SimpleNamespace(metadata_path=str(metadata_path), shelves=[])
	scanned_shelves = [
		SimpleNamespace(
			shelf_idx=0,
			cells=[
				SimpleNamespace(cell_idx=0, calibration_point=SimpleNamespace(x=340.0, y=493.0, z_offset=0.0)),
				SimpleNamespace(cell_idx=1, calibration_point=SimpleNamespace(x=474.0, y=493.0, z_offset=0.0)),
			],
		)
	]
	# cell 0 has a wall depth, cell 1 does not
	partial_lookup = {(0, 0): SimpleNamespace(wall_depth_mm=9.9)}

	with patch('openshelf_os.laser_calibration.staged_scan.build_candidate_lookup_by_cell', return_value=partial_lookup):
		with pytest.raises(ValueError, match='no laser wall depth'):
			apply_wall_depth_z_offsets(
				analysis=analysis,
				scanned_shelves=scanned_shelves,
				metadata_path=str(metadata_path),
				z_delta_mm=4.85,
				z_delta_slope_per_mm=-0.00285,
			)

	# the cell that did have a wall depth was still computed, not left blank
	assert scanned_shelves[0].cells[0].calibration_point.z_offset == pytest.approx(9.9 + 4.85 - 0.00285 * 340.0)




def test_scan_rebuild_leaves_matched_and_new_cells_unsized_but_keeps_manual_cells() -> None:
	analysis = SimpleNamespace(shelves=[_detected_scan_shelf(0, [329.0, 494.0, 660.0])])
	existing = _existing_calibrated_shelf(0, [329.0, 494.0])
	for cell in existing.cells:
		cell.dimensions = Dimensions(width=145.0, height=115.0, length=185.0)
	existing.cells[1].is_manual = True

	result = build_shelves_from_scan(
		side=StorageModuleSide.FRONT,
		analysis=analysis,
		existing_shelves=[existing],
	)

	cells = sorted(result[0].cells, key=lambda cell: cell.cell_idx)
	unsized = Dimensions(width=0.0, height=0.0, length=0.0)
	assert cells[0].dimensions == unsized
	assert cells[1].dimensions == Dimensions(width=145.0, height=115.0, length=185.0)
	assert cells[2].dimensions == unsized


def test_single_cell_rescan_leaves_the_cell_unsized(tmp_path: Path) -> None:
	from openshelf_os.laser_calibration import apply_single_cell_rescan
	from openshelf_os.laser_calibration.core import SingleCellRescanTarget
	from openshelf_os.storage import StorageGrid

	shelf = _existing_calibrated_shelf(0, [329.0])
	shelf.cells[0].dimensions = Dimensions(width=145.0, height=115.0, length=185.0)
	grid = StorageGrid(filename=str(tmp_path / "storage.json"), front_module=[shelf], back_module=[])

	apply_single_cell_rescan(
		grid=grid,
		target=SingleCellRescanTarget(shelf_idx=0, cell_idx=0),
		side=StorageModuleSide.FRONT,
		detected_cell=_detected_scan_shelf(0, [330.0]).cells[0],
		scan_config=SimpleNamespace(front_gripper_xy_correction_mm=None),
	)

	assert grid.front_module[0].cells[0].dimensions == Dimensions(width=0.0, height=0.0, length=0.0)
