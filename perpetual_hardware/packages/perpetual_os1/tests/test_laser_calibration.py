import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from perpetual_os1.common import LaserScanConfig
from perpetual_os1.laser_calibration import (
	apply_xy_reference_offsets,
	apply_single_shelf_analysis,
	apply_single_cell_rescan,
	build_single_shelf_known_edge_y,
	build_single_cell_rescan_candidate,
	build_run_parameters,
	capture_shelf_candidates,
	enter_scan_mode,
	estimate_seeded_candidate_wall_depths,
	find_first_positive_run_segment,
	find_gap_segments_in_line,
	find_gap_run_containing_position,
	GapCandidateRegion,
	get_scanned_anchor_cell,
	GridScanResult,
	load_continue_scan_context,
	run_io_box_scout,
	run_staged_scan,
	run_with_temp_robot_config,
	scan_shelf_scout_line,
	RIGHT_MODULE_ROTATION_SAFE_X_MM,
	SingleCellRescanTarget,
)
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageGrid, StorageModuleSide


def test_laser_scan_config_uses_clear_field_names() -> None:
	scan_config = LaserScanConfig.model_validate(
		{
			"fine_scan_step_mm": 0.5,
			"laser_sample_settle_ms": 40,
			"laser_samples_per_point": 2,
			"laser_scan_stage_extension_mm": 13.0,
			"laser_scan_z_offset_from_place_mm": 10.0,
			"shelf_hole_probe_y_offsets_mm": [32.25],
			"fine_cell_scan_y_margin_mm": 12.0,
		}
	)

	assert scan_config.fine_scan_step_mm == pytest.approx(0.5)
	assert scan_config.laser_sample_settle_ms == 40
	assert scan_config.laser_samples_per_point == 2
	assert scan_config.laser_scan_stage_extension_mm == pytest.approx(13.0)
	assert scan_config.laser_scan_z_offset_from_place_mm == pytest.approx(10.0)
	assert scan_config.shelf_hole_probe_y_offsets_mm == [pytest.approx(32.25)]
	assert scan_config.fine_cell_scan_y_margin_mm == pytest.approx(12.0)


def test_laser_scan_config_rejects_legacy_field_names() -> None:
	with pytest.raises(ValueError, match="Legacy laser scan config keys are not supported"):
		LaserScanConfig.model_validate(
			{
				"step_mm": 0.5,
				"settle_ms": 40,
				"left_z_reference": {
					"shelf_idx": 12,
					"cell_idx": 0,
					"z_offset_mm": 15.0,
					"wall_depth_mm": 6.5,
				},
			}
		)


def test_find_gap_segments_in_line_matches_expected_hole_width() -> None:
	positions = [float(idx * 4) for idx in range(12)]
	values = [10.0, 10.0, 10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
	scan_config = SimpleNamespace(
		expected_hole_width_mm=26.0,
		hole_width_tolerance_mm=2.0,
		wall_depth_tolerance_mm=1.0,
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

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.capture_shelf_candidates', side_effect=didCaptureShelfCandidates):
			with patch('perpetual_os1.laser_calibration.persist_staged_metadata'):
				with patch('perpetual_os1.laser_calibration.persist_scan_started_at'):
					task = asyncio.create_task(
						run_staged_scan(
							robot=robot,
							side=StorageModuleSide.LEFT,
							bounds=ScanBounds(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
							fine_step_mm=1.0,
							settle_ms=0,
							samples_per_point=1,
							coarse_settle_ms=0,
							coarse_samples_per_point=1,
							scan_config=SimpleNamespace(
								laser_offset_x_mm=1.0,
								laser_offset_y_mm=0.0,
								left_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=5.0, y_mm=5.0, z_offset_mm=2.0, wall_depth_mm=1.0),
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

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.capture_shelf_candidates', side_effect=didCaptureShelfCandidates):
			with patch('perpetual_os1.laser_calibration.persist_staged_metadata'):
				with patch('perpetual_os1.laser_calibration.persist_scan_started_at'):
					with pytest.raises(asyncio.CancelledError):
						await run_staged_scan(
							robot=robot,
							side=StorageModuleSide.LEFT,
							bounds=ScanBounds(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
							fine_step_mm=1.0,
							settle_ms=0,
							samples_per_point=1,
							coarse_settle_ms=0,
							coarse_samples_per_point=1,
							scan_config=SimpleNamespace(
								laser_offset_x_mm=1.0,
								laser_offset_y_mm=0.0,
								left_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=5.0, y_mm=5.0, z_offset_mm=2.0, wall_depth_mm=1.0),
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
async def test_run_staged_scan_uses_candidate_wall_depth_for_fine_scan(tmp_path: Path) -> None:
	should_abort = False
	tool = SimpleNamespace(retract=AsyncMock())
	robot = SimpleNamespace(_tool=tool)
	candidate = GapCandidateRegion(
		x_min=10.0,
		x_max=30.0,
		y_min=40.0,
		y_max=40.0,
		wall_depth_mm=4.61,
	)
	scan_config = SimpleNamespace(
		expected_hole_height_mm=30.0,
		fine_cell_scan_y_margin_mm=None,
		laser_offset_x_mm=1.0,
		laser_offset_y_mm=0.0,
		left_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=50.0, y_mm=50.0, z_offset_mm=2.0, wall_depth_mm=1.0),
	)

	async def didCaptureCrossScan(*args, **kwargs):
		nonlocal should_abort
		should_abort = True

		return GridScanResult(x_positions=[], y_positions=[], depth_rows=[]), []

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.set_y_brake_for_scan_motion', AsyncMock()):
			with patch(
				'perpetual_os1.laser_calibration.capture_shelf_candidates',
				AsyncMock(return_value=(GridScanResult(x_positions=[], y_positions=[], depth_rows=[]), 4.16, [candidate], [])),
			):
				with patch('perpetual_os1.laser_calibration.capture_cross_scan', side_effect=didCaptureCrossScan) as capture_cross_scan:
					with pytest.raises(asyncio.CancelledError):
						await run_staged_scan(
							robot=robot,
							side=StorageModuleSide.LEFT,
							bounds=ScanBounds(x_min=0.0, x_max=100.0, y_min=0.0, y_max=100.0),
							fine_step_mm=1.0,
							settle_ms=0,
							samples_per_point=1,
							coarse_settle_ms=0,
							coarse_samples_per_point=1,
							scan_config=scan_config,
							scans_dir=str(tmp_path),
							run_parameters={},
							shelf_scout_only=False,
							known_shelf_edge_y=None,
							should_abort=lambda: should_abort,
						)

	capture_cross_scan.assert_awaited_once()
	assert capture_cross_scan.await_args.kwargs['wall_depth_mm'] == pytest.approx(4.61)


@pytest.mark.asyncio
async def test_scan_shelf_scout_line_passes_abort_to_gantry_wait() -> None:
	should_abort = False

	gantry_wait = AsyncMock(side_effect=asyncio.CancelledError())
	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		gantry_wait=gantry_wait,
	)

	with pytest.raises(asyncio.CancelledError):
		await scan_shelf_scout_line(
			tool,
			x_mm=10.0,
			y_positions=[100.0, 99.0],
			settle_ms=0,
			samples_per_point=1,
			progress_label='shelf_scout_1',
			should_abort=lambda: should_abort,
		)

	assert gantry_wait.await_args.kwargs['should_abort'] is not None


@pytest.mark.asyncio
async def test_enter_scan_mode_uses_safe_x_for_right_module() -> None:
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
		side=StorageModuleSide.RIGHT,
		scan_config=SimpleNamespace(),
	)

	first_move = tool.move.await_args_list[0].args[0]
	second_move = tool.move.await_args_list[1].args[0]
	assert first_move.x == pytest.approx(RIGHT_MODULE_ROTATION_SAFE_X_MM)
	assert first_move.y == pytest.approx(1200.0)
	assert first_move.s == 1
	assert second_move.x == pytest.approx(900.0)
	assert second_move.y == pytest.approx(1200.0)
	assert second_move.s == 1


@pytest.mark.asyncio
async def test_capture_shelf_candidates_does_not_append_floor_edge_for_empty_detected_edges(
	tmp_path: Path,
) -> None:
	coarse_scan, wall_depth_mm, candidates, shelf_edges = await capture_shelf_candidates(
		SimpleNamespace(),
		side=StorageModuleSide.LEFT,
		bounds=ScanBounds(x_min=0.0, x_max=10.0, y_min=0.0, y_max=100.0),
		settle_ms=0,
		samples_per_point=1,
		scan_config=SimpleNamespace(),
		recorder=SimpleNamespace(coarse_depth_path=tmp_path / 'coarse_depth.npy'),
		shelf_scout_only=True,
		known_shelf_edge_y=[],
	)

	assert len(coarse_scan.depth_rows) > 0
	assert wall_depth_mm == pytest.approx(0.0)
	assert candidates == []
	assert shelf_edges == []


def test_find_first_positive_run_segment_handles_descending_positions() -> None:
	segment = find_first_positive_run_segment(
		values=[0.0, 0.0, 4.0, 4.1, 4.0, 0.0],
		positions=[1250.0, 1245.0, 1240.0, 1235.0, 1230.0, 1225.0],
		step_mm=5.0,
	)

	assert segment is not None
	assert segment.start_idx == 2
	assert segment.end_idx == 4
	assert segment.start_mm == pytest.approx(1227.5)
	assert segment.end_mm == pytest.approx(1242.5)
	assert segment.center_mm == pytest.approx(1235.0)


def test_find_gap_run_containing_position_returns_gap_bounds() -> None:
	segment = find_gap_run_containing_position(
		values=[5.0, 0.0, 0.0, 0.0, 5.0],
		positions=[10.0, 11.0, 12.0, 13.0, 14.0],
		step_mm=1.0,
		seed_mm=12.0,
	)

	assert segment is not None
	assert segment.start_idx == 1
	assert segment.end_idx == 3
	assert segment.start_mm == pytest.approx(10.5)
	assert segment.end_mm == pytest.approx(13.5)
	assert segment.center_mm == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_run_io_box_scout_derives_center_from_measured_right_edge() -> None:
	y_motor = SimpleNamespace(
		disable_brake_output=AsyncMock(),
		restore_brake_output=AsyncMock(),
	)
	tool = SimpleNamespace(
		retract=AsyncMock(),
		get_param=AsyncMock(return_value=40.0),
		_my=y_motor,
	)
	io_box = SimpleNamespace(open_door=AsyncMock(), home_door=AsyncMock())
	robot = SimpleNamespace(_tool=tool, _io=io_box)

	async def didScanLine(tool, *, axis, positions, fixed_mm, **kwargs):
		if kwargs['progress_label'] == 'io_box_coarse_y':
			return [5.0 if 70.0 <= position <= 80.0 else 0.0 for position in positions]

		if kwargs['progress_label'] in {'io_box_fine_y', 'io_box_final_y'}:
			return [0.0 if 65.0 <= position <= 85.0 else 5.0 for position in positions]

		if kwargs['progress_label'] == 'io_box_fine_x':
			return [0.0 if position <= 70.0 else 5.0 for position in positions]

		raise AssertionError(kwargs['progress_label'])

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.scan_line_positions', side_effect=didScanLine):
			result = await run_io_box_scout(
				robot=robot,
				scan_config=SimpleNamespace(laser_offset_x_mm=10.0, laser_offset_y_mm=2.0),
				io_box_interior_width_mm=120.0,
				start_x_mm=0.0,
				start_y_mm=100.0,
				y_min_mm=0.0,
				x_max_mm=100.0,
				coarse_step_mm=10.0,
				fine_step_mm=1.0,
				fine_margin_mm=10.0,
				settle_ms=0,
				samples_per_point=1,
				laser_offset_x_mm=10.0,
				laser_offset_y_mm=2.0,
			)

	assert result.measured_x_min_mm is None
	assert result.measured_x_max_mm == pytest.approx(70.5)
	assert result.derived_x_min_mm == pytest.approx(-49.5)
	assert result.laser_center_x_mm == pytest.approx(10.5)
	assert result.laser_center_y_mm == pytest.approx(75.0)
	assert result.axis_center_x_mm == pytest.approx(20.5)
	assert result.axis_center_y_mm == pytest.approx(77.0)
	assert result.laser_offset_x_mm == pytest.approx(10.0)
	assert result.laser_offset_y_mm == pytest.approx(2.0)
	assert result.calibration_low_x_mm == pytest.approx(20.5)
	assert result.calibration_low_y_mm == pytest.approx(101.5)
	assert result.calibration_high_x_mm == pytest.approx(20.5)
	assert result.calibration_high_y_mm == pytest.approx(7.5)
	assert result.stage_extension_mm == pytest.approx(45.0)
	assert result.z_extension_mm == pytest.approx(163.5)
	assert result.final_y_refined is True
	io_box.open_door.assert_awaited_once()
	io_box.home_door.assert_awaited_once()
	tool.retract.assert_awaited_once()
	assert y_motor.disable_brake_output.await_count == 3
	assert y_motor.restore_brake_output.await_count == 3


@pytest.mark.asyncio
async def test_run_io_box_scout_derives_stage_and_z_extension() -> None:
	y_motor = SimpleNamespace(
		disable_brake_output=AsyncMock(),
		restore_brake_output=AsyncMock(),
	)
	tool = SimpleNamespace(
		retract=AsyncMock(),
		get_param=AsyncMock(return_value=83.0),
		_my=y_motor,
	)
	io_box = SimpleNamespace(open_door=AsyncMock(), home_door=AsyncMock())
	robot = SimpleNamespace(_tool=tool, _io=io_box)

	async def didScanLine(tool, *, axis, positions, fixed_mm, **kwargs):
		if kwargs['progress_label'] == 'io_box_coarse_y':
			return [5.0 if 70.0 <= position <= 80.0 else 0.0 for position in positions]

		if kwargs['progress_label'] in {'io_box_fine_y', 'io_box_final_y'}:
			return [0.0 if 65.0 <= position <= 85.0 else 5.0 for position in positions]

		if kwargs['progress_label'] == 'io_box_fine_x':
			return [0.0 if position <= 70.0 else 5.0 for position in positions]

		raise AssertionError(kwargs['progress_label'])

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.scan_line_positions', side_effect=didScanLine):
			result = await run_io_box_scout(
				robot=robot,
				scan_config=SimpleNamespace(laser_offset_x_mm=0.0, laser_offset_y_mm=0.0),
				io_box_interior_width_mm=120.0,
				start_x_mm=0.0,
				start_y_mm=100.0,
				y_min_mm=0.0,
				x_max_mm=100.0,
				coarse_step_mm=10.0,
				fine_step_mm=1.0,
				fine_margin_mm=10.0,
				settle_ms=0,
				samples_per_point=1,
			)

	assert result.calibration_low_x_mm == pytest.approx(10.5)
	assert result.calibration_low_y_mm == pytest.approx(99.5)
	assert result.stage_extension_mm == pytest.approx(88.0)
	assert result.z_extension_mm == pytest.approx(163.5)
	tool.get_param.assert_awaited_once_with('STAGE_EXTENSION_SHELF_FRONT')


@pytest.mark.asyncio
async def test_run_io_box_scout_stops_x_scan_at_width_limit() -> None:
	y_motor = SimpleNamespace(
		disable_brake_output=AsyncMock(),
		restore_brake_output=AsyncMock(),
	)
	tool = SimpleNamespace(retract=AsyncMock(), _my=y_motor)
	io_box = SimpleNamespace(open_door=AsyncMock(), home_door=AsyncMock())
	robot = SimpleNamespace(_tool=tool, _io=io_box)

	async def didScanLine(tool, *, axis, positions, fixed_mm, **kwargs):
		if kwargs['progress_label'] == 'io_box_coarse_y':
			return [5.0 if 70.0 <= position <= 80.0 else 0.0 for position in positions]

		if kwargs['progress_label'] == 'io_box_fine_y':
			return [0.0 if 65.0 <= position <= 85.0 else 5.0 for position in positions]

		if kwargs['progress_label'] == 'io_box_fine_x':
			assert max(positions) == pytest.approx(65.0)
			return [0.0 for _ in positions]

		raise AssertionError(kwargs['progress_label'])

	with patch('perpetual_os1.laser_calibration.enter_scan_mode', AsyncMock()):
		with patch('perpetual_os1.laser_calibration.scan_line_positions', side_effect=didScanLine):
			with pytest.raises(ValueError, match='interior width limit'):
				await run_io_box_scout(
					robot=robot,
					scan_config=SimpleNamespace(laser_offset_x_mm=0.0, laser_offset_y_mm=0.0),
					io_box_interior_width_mm=65.0,
					start_x_mm=0.0,
					start_y_mm=100.0,
					y_min_mm=0.0,
					x_max_mm=100.0,
					coarse_step_mm=10.0,
					fine_step_mm=1.0,
					fine_margin_mm=10.0,
					settle_ms=0,
					samples_per_point=1,
				)

	io_box.open_door.assert_awaited_once()
	io_box.home_door.assert_awaited_once()
	tool.retract.assert_awaited_once()


def test_get_scanned_anchor_cell_uses_physical_top_left_for_right_side() -> None:
	bottom_shelf = SimpleNamespace(
		shelf_idx=0,
		cells=[
			SimpleNamespace(calibration_point=SimpleNamespace(x=-100.0, y=100.0)),
			SimpleNamespace(calibration_point=SimpleNamespace(x=0.0, y=100.0)),
		],
	)
	top_shelf = SimpleNamespace(
		shelf_idx=1,
		cells=[
			SimpleNamespace(calibration_point=SimpleNamespace(x=-578.0, y=1138.0)),
			SimpleNamespace(calibration_point=SimpleNamespace(x=51.5, y=1143.0)),
		],
	)

	anchor_cell = get_scanned_anchor_cell(
		scanned_shelves=[bottom_shelf, top_shelf],
		side=StorageModuleSide.RIGHT,
		anchor='top_left',
	)

	assert anchor_cell is not None
	assert anchor_cell.calibration_point.x == pytest.approx(-578.0)


def test_apply_xy_reference_offsets_uses_physical_top_left_for_right_side() -> None:
	scanned_shelves = [
		SimpleNamespace(
			shelf_idx=1,
			cells=[
				SimpleNamespace(calibration_point=SimpleNamespace(x=-578.0, y=1138.0)),
				SimpleNamespace(calibration_point=SimpleNamespace(x=51.5, y=1143.0)),
			],
		)
	]
	scan_config = SimpleNamespace(
		right_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=51.5, y_mm=1143.0, z_offset_mm=1.0, wall_depth_mm=1.0),
	)

	apply_xy_reference_offsets(
		scanned_shelves=scanned_shelves,
		scan_config=scan_config,
		side=StorageModuleSide.RIGHT,
	)

	assert scanned_shelves[0].cells[0].calibration_point.x == pytest.approx(51.5)
	assert scanned_shelves[0].cells[0].calibration_point.y == pytest.approx(1143.0)
	assert scanned_shelves[0].cells[1].calibration_point.x == pytest.approx(681.0)
	assert scanned_shelves[0].cells[1].calibration_point.y == pytest.approx(1148.0)


def test_apply_xy_reference_offsets_requires_reference() -> None:
	with pytest.raises(ValueError, match='robot.grid.scan.left_xy_reference must be configured'):
		apply_xy_reference_offsets(
			scanned_shelves=[],
			scan_config=SimpleNamespace(left_reference=None),
			side=StorageModuleSide.LEFT,
		)


def test_build_single_cell_rescan_candidate_requires_z_reference() -> None:
	target_cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=100.0, y=200.0, z_offset=8.5),
	)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_reference=None,
	)

	with pytest.raises(ValueError, match='robot.grid.scan.left_z_reference must be configured'):
		build_single_cell_rescan_candidate(
			grid=grid,
			side=StorageModuleSide.LEFT,
			target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
			scan_config=scan_config,
		)


def test_build_single_cell_rescan_candidate_requires_laser_offsets() -> None:
	target_cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=100.0, y=200.0, z_offset=8.5),
	)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=None,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_reference=SimpleNamespace(shelf_idx=12, cell_idx=0, anchor='top_right', x_mm=0.0, y_mm=0.0, z_offset_mm=15.0, wall_depth_mm=9.0),
	)

	with pytest.raises(ValueError, match='robot.grid.scan.laser_offset_y_mm must be configured'):
		build_single_cell_rescan_candidate(
			grid=grid,
			side=StorageModuleSide.LEFT,
			target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
			scan_config=scan_config,
		)


def test_build_single_cell_rescan_candidate_rejects_invalid_offset_value() -> None:
	target_cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=100.0, y=200.0, z_offset=8.5),
	)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=float('nan'),
		laser_offset_y_mm=0.0,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_reference=SimpleNamespace(shelf_idx=12, cell_idx=0, anchor='top_right', x_mm=0.0, y_mm=0.0, z_offset_mm=15.0, wall_depth_mm=9.0),
	)

	with pytest.raises(ValueError, match='robot.grid.scan.laser_offset_x_mm must be a finite number'):
		build_single_cell_rescan_candidate(
			grid=grid,
			side=StorageModuleSide.LEFT,
			target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
			scan_config=scan_config,
		)


def test_build_single_cell_rescan_candidate_allows_scan_estimated_wall_depth() -> None:
	target_cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=100.0, y=200.0, z_offset=0.0),
	)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_reference=SimpleNamespace(shelf_idx=12, cell_idx=0, anchor='top_right', x_mm=0.0, y_mm=0.0, z_offset_mm=15.0),
	)

	candidate = build_single_cell_rescan_candidate(
		grid=grid,
		side=StorageModuleSide.LEFT,
		target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
		scan_config=scan_config,
	)

	assert candidate.wall_depth_mm is None


def test_build_run_parameters_allows_rescan_bounds_away_from_xy_reference() -> None:
	args = SimpleNamespace(
		module='left',
		x=None,
		y=None,
		y_offset=0.0,
		settle_ms=None,
		samples_per_point=None,
		known_shelf_edge_y=None,
		npy=None,
		scans_dir='scans',
		config='config.json',
		apply=True,
		shelf_scout_only=False,
		rescan_cell='1:2',
	)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=50.0,
		laser_offset_y_mm=0.0,
		left_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=900.0, y_mm=900.0, z_offset_mm=15.0, wall_depth_mm=9.0),
	)

	run_parameters = build_run_parameters(
		args=args,
		side=StorageModuleSide.LEFT,
		bounds=ScanBounds(x_min=40.0, x_max=80.0, y_min=40.0, y_max=80.0),
		step_mm=2.0,
		settle_ms=0,
		samples_per_point=1,
		coarse_settle_ms=0,
		coarse_samples_per_point=1,
		scan_config=scan_config,
	)

	assert run_parameters['resolved']['xy_reference']['x_mm'] == pytest.approx(900.0)


def test_build_run_parameters_rejects_full_scan_bounds_away_from_xy_reference() -> None:
	args = SimpleNamespace(
		module='left',
		x=(40.0, 80.0),
		y=(40.0, 80.0),
		y_offset=0.0,
		settle_ms=None,
		samples_per_point=None,
		known_shelf_edge_y=None,
		npy=None,
		scans_dir='scans',
		config='config.json',
		apply=True,
		shelf_scout_only=False,
		rescan_cell=None,
	)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=50.0,
		laser_offset_y_mm=0.0,
		left_reference=SimpleNamespace(shelf_idx=0, cell_idx=0, anchor='top_left', x_mm=900.0, y_mm=900.0, z_offset_mm=15.0, wall_depth_mm=9.0),
	)

	with pytest.raises(ValueError, match='xy_reference x_mm resolves outside scan X bounds'):
		build_run_parameters(
			args=args,
			side=StorageModuleSide.LEFT,
			bounds=ScanBounds(x_min=40.0, x_max=80.0, y_min=40.0, y_max=80.0),
			step_mm=2.0,
			settle_ms=0,
			samples_per_point=1,
			coarse_settle_ms=0,
			coarse_samples_per_point=1,
			scan_config=scan_config,
		)


def test_load_continue_scan_context_reads_latest_incomplete_run(tmp_path: Path) -> None:
	completed_run_dir = tmp_path / 'completed-run'
	completed_run_dir.mkdir()
	(completed_run_dir / 'run_parameters.json').write_text(
		json.dumps(
			{
				'run_parameters': {
					'module': 'left',
					'resolved': {
						'bounds': {'x_min': 0.0, 'x_max': 1.0, 'y_min': 0.0, 'y_max': 1.0},
						'fine_step_mm': 1.0,
						'settle_ms': 10,
						'samples_per_point': 1,
					}
				}
			}
		),
		encoding='utf-8',
	)
	(completed_run_dir / 'metadata.json').write_text(
		json.dumps({'status': 'completed'}),
		encoding='utf-8',
	)

	incomplete_run_dir = tmp_path / 'incomplete-run'
	incomplete_run_dir.mkdir()
	(incomplete_run_dir / 'run_parameters.json').write_text(
		json.dumps(
			{
				'run_parameters': {
					'module': StorageModuleSide.RIGHT.value,
					'resolved': {
						'bounds': {'x_min': 10.0, 'x_max': 20.0, 'y_min': 30.0, 'y_max': 40.0},
						'fine_step_mm': 2.5,
						'settle_ms': 25,
						'samples_per_point': 3,
						'coarse_settle_ms': 12,
						'coarse_samples_per_point': 2,
					}
				}
			}
		),
		encoding='utf-8',
	)
	(incomplete_run_dir / 'metadata.json').write_text(
		json.dumps({'status': 'fine-scan'}),
		encoding='utf-8',
	)

	recorder, side, bounds, step_mm, settle_ms, samples_per_point, coarse_settle_ms, coarse_samples_per_point = load_continue_scan_context(str(tmp_path))

	assert recorder.run_dir == incomplete_run_dir
	assert side == StorageModuleSide.RIGHT
	assert bounds == ScanBounds(x_min=10.0, x_max=20.0, y_min=30.0, y_max=40.0)
	assert step_mm == pytest.approx(2.5)
	assert settle_ms == 25
	assert samples_per_point == 3
	assert coarse_settle_ms == 12
	assert coarse_samples_per_point == 2


def test_build_single_cell_rescan_candidate_uses_target_point_without_z_depth_seed() -> None:
	target_cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=100.0, y=200.0, z_offset=8.5),
	)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_reference=SimpleNamespace(shelf_idx=12, cell_idx=0, anchor='top_right', x_mm=0.0, y_mm=0.0, z_offset_mm=15.0, wall_depth_mm=9.0),
		right_reference=None,
	)

	candidate = build_single_cell_rescan_candidate(
		grid=grid,
		side=StorageModuleSide.LEFT,
		target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
		scan_config=scan_config,
	)

	assert candidate.x_min == pytest.approx(36.0)
	assert candidate.x_max == pytest.approx(62.0)
	assert candidate.y_min == pytest.approx(185.0)
	assert candidate.y_max == pytest.approx(215.0)
	assert candidate.wall_depth_mm is None


@pytest.mark.asyncio
async def test_estimate_seeded_candidate_wall_depth_initializes_empty_coarse_scan() -> None:
	candidate = GapCandidateRegion(
		x_min=12.0,
		x_max=28.0,
		y_min=20.0,
		y_max=40.0,
		wall_depth_mm=None,
	)
	coarse_scan = GridScanResult(x_positions=[], y_positions=[], depth_rows=[])
	scan_config = SimpleNamespace(
		expected_hole_width_mm=26.0,
		hole_width_tolerance_mm=4.0,
		wall_depth_tolerance_mm=1.0,
	)

	async def didScanLinePositions(*args, **kwargs):
		positions = kwargs['positions']
		return [10.0 if position < 12.0 or position > 32.0 else 0.0 for position in positions]

	with patch('perpetual_os1.laser_calibration.staged_scan.scan_line_positions', side_effect=didScanLinePositions):
		await estimate_seeded_candidate_wall_depths(
			SimpleNamespace(),
			bounds=ScanBounds(x_min=0.0, x_max=40.0, y_min=0.0, y_max=60.0),
			candidates=[candidate],
			settle_ms=0,
			samples_per_point=1,
			scan_config=scan_config,
			coarse_scan=coarse_scan,
		)

	assert candidate.wall_depth_mm == pytest.approx(10.0)
	assert len(coarse_scan.x_positions) > 0
	assert len(coarse_scan.y_positions) > 0


def test_build_single_shelf_known_edge_y_uses_calibrated_cells() -> None:
	target_shelf = SimpleNamespace(
		cells=[
			SimpleNamespace(calibration_point=SimpleNamespace(y=200.0)),
			SimpleNamespace(calibration_point=SimpleNamespace(y=206.0)),
		],
	)
	grid = SimpleNamespace(get_shelf=lambda side, shelf_idx: target_shelf)
	scan_config = SimpleNamespace(
		laser_offset_y_mm=4.0,
		shelf_hole_probe_y_offsets_mm=[32.25],
	)

	known_shelf_edge_y = build_single_shelf_known_edge_y(
		grid=grid,
		side=StorageModuleSide.LEFT,
		shelf_idx=2,
		scan_config=scan_config,
	)

	assert known_shelf_edge_y == [pytest.approx(174.75)]


def test_apply_single_cell_rescan_updates_target_cell_and_saves_grid() -> None:
	calibration_point = SimpleNamespace(x=1.0, y=2.0, z_offset=3.0)
	target_cell = SimpleNamespace(calibration_point=calibration_point)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(
		get_shelf=lambda side, shelf_idx: target_shelf,
		save=Mock(),
	)
	scan_config = SimpleNamespace(
		left_reference=SimpleNamespace(shelf_idx=12, cell_idx=0, anchor='top_right', x_mm=0.0, y_mm=0.0, z_offset_mm=15.0, wall_depth_mm=11.0),
		right_reference=None,
	)
	detected_cell = SimpleNamespace(tool_x_mm=123.0, tool_y_mm=456.0)

	apply_single_cell_rescan(
		grid=grid,
		target=SingleCellRescanTarget(shelf_idx=0, cell_idx=1),
		side=StorageModuleSide.LEFT,
		detected_cell=detected_cell,
		wall_depth_mm=7.5,
		scan_config=scan_config,
	)

	assert calibration_point.x == pytest.approx(123.0)
	assert calibration_point.y == pytest.approx(456.0)
	assert calibration_point.z_offset == pytest.approx(3.0)
	grid.save.assert_called_once_with()


def test_apply_single_shelf_analysis_replaces_only_target_shelf(tmp_path: Path) -> None:
	target_shelf = Shelf(
		shelf_idx=1,
		shelf_height=100.0,
		shelf_width=200.0,
		divider_width=13.0,
		cells=[
			Cell(
				cell_idx=0,
				status=CellStatus.AVAILABLE,
				item=None,
				calibration_point=CalibrationCoordinate(x=10.0, y=20.0, z_offset=30.0, s=0),
				dimensions=Dimensions(width=40.0, height=50.0, length=60.0),
			)
		],
	)
	grid_path = tmp_path / 'grid.json'
	grid = StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_height=10.0,
				shelf_width=20.0,
				cells=[],
			),
			target_shelf,
			Shelf(
				shelf_idx=2,
				shelf_height=30.0,
				shelf_width=40.0,
				cells=[],
			),
		],
		right_module=[],
		filename=str(grid_path),
	)
	grid.save()

	scanned_shelf = target_shelf.model_copy(deep=True, update={'shelf_idx': 0, 'shelf_height': 333.0})
	scanned_shelf.divider_width = 99.0
	analysis = SimpleNamespace(
		shelves=[SimpleNamespace()],
		metadata_path=str(tmp_path / 'metadata.json'),
	)

	with patch('perpetual_os1.laser_calibration.build_shelves_from_scan', return_value=[scanned_shelf]):
		with patch('perpetual_os1.laser_calibration.staged_scan.apply_xy_reference_offsets') as apply_xy_reference_offsets_mock:
			with patch('perpetual_os1.laser_calibration.apply_wall_depth_z_offsets'):
				apply_single_shelf_analysis(
					analysis=analysis,
					grid=grid,
					grid_path=grid_path,
					scan_config=SimpleNamespace(
						left_xy_reference=SimpleNamespace(
							shelf_idx=0,
							cell_idx=0,
							anchor='top_left',
							x_mm=10.0,
							y_mm=20.0,
						),
					),
					side=StorageModuleSide.LEFT,
					target_shelf_idx=1,
				)
		apply_xy_reference_offsets_mock.assert_not_called()

	updated_grid = StorageGrid.load_from_file(str(grid_path))

	assert updated_grid.left_module[0].shelf_idx == 0
	assert updated_grid.left_module[1].shelf_idx == 1
	assert updated_grid.left_module[1].shelf_height == pytest.approx(333.0)
	assert updated_grid.left_module[1].divider_width == pytest.approx(13.0)
	assert updated_grid.left_module[2].shelf_idx == 2


@pytest.mark.asyncio
async def test_run_with_temp_robot_config_skips_stop_when_cancelled(tmp_path: Path) -> None:
	robot = SimpleNamespace(
		configure=AsyncMock(),
		stop=AsyncMock(),
		shutdown=AsyncMock(),
	)
	started = asyncio.Event()

	async def didRun(current_robot) -> None:
		assert current_robot is robot
		started.set()
		await asyncio.sleep(10)

	with patch('perpetual_os1.laser_calibration.build_standalone_config', return_value={}):
		with patch('perpetual_os1.laser_calibration.Robot', return_value=robot):
			task = asyncio.create_task(
				run_with_temp_robot_config(
					config=SimpleNamespace(),
					config_path=str(tmp_path / 'config.json'),
					didRun=didRun,
					shutdown_action='stop',
				)
			)

			await started.wait()
			task.cancel()

			with pytest.raises(asyncio.CancelledError):
				await task

	robot.stop.assert_not_awaited()
	robot.shutdown.assert_not_awaited()
