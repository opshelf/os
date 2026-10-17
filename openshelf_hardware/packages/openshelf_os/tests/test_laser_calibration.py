import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openshelf_os.laser_calibration import (
	require_z_offset_model_matches_z_place,
	apply_wall_depth_z_offsets,
	disable_unreachable_cells,
	run_staged_scan,
)
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageModuleSide


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
