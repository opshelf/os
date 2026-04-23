import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from perpetual_os1.laser_calibration import (
	apply_xy_reference_offsets,
	apply_single_cell_rescan,
	build_single_cell_rescan_candidate,
	capture_shelf_candidates,
	get_scanned_anchor_cell,
	load_continue_scan_context,
	run_staged_scan,
	run_with_temp_robot_config,
	scan_shelf_scout_line,
	SingleCellRescanTarget,
)
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import StorageModuleSide


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
							scan_config=SimpleNamespace(),
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
							scan_config=SimpleNamespace(),
							scans_dir=str(tmp_path),
							run_parameters={},
							shelf_scout_only=False,
							known_shelf_edge_y=None,
							recorder=recorder,
							should_abort=lambda: should_abort,
						)

	tool.retract.assert_not_awaited()


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
		right_xy_reference=SimpleNamespace(anchor='top_left', x_mm=51.5, y_mm=1143.0),
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


def test_build_single_cell_rescan_candidate_uses_target_point_and_z_delta() -> None:
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
		left_z_delta_mm=6.0,
		right_z_delta_mm=None,
		left_z_reference=None,
		right_z_reference=None,
	)

	candidate = build_single_cell_rescan_candidate(
		grid=grid,
		side=StorageModuleSide.LEFT,
		target=SingleCellRescanTarget(shelf_idx=1, cell_idx=2),
		scan_config=scan_config,
	)

	assert candidate.x_min == pytest.approx(138.0)
	assert candidate.x_max == pytest.approx(164.0)
	assert candidate.y_min == pytest.approx(185.0)
	assert candidate.y_max == pytest.approx(215.0)
	assert candidate.wall_depth_mm == pytest.approx(2.5)


def test_apply_single_cell_rescan_updates_target_cell_and_saves_grid() -> None:
	calibration_point = SimpleNamespace(x=1.0, y=2.0, z_offset=3.0)
	target_cell = SimpleNamespace(calibration_point=calibration_point)
	target_shelf = SimpleNamespace(get_cell=lambda cell_idx: target_cell)
	grid = SimpleNamespace(
		get_shelf=lambda side, shelf_idx: target_shelf,
		save=Mock(),
	)
	scan_config = SimpleNamespace(
		left_z_delta_mm=4.0,
		right_z_delta_mm=None,
		left_z_reference=None,
		right_z_reference=None,
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
	assert calibration_point.z_offset == pytest.approx(11.5)
	grid.save.assert_called_once_with()


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
