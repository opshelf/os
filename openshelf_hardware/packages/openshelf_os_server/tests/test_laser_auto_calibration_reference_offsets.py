"""The reference-frame derivation path of the automatic laser calibration."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os import laser_calibration as laser_module
from openshelf_os.common import Parameters
from openshelf_os.laser_calibration.reference_offsets import ReferenceOffsetResult
from openshelf_os.laser_calibration.scan import ScanDetectedCell, ScanDetectedShelf
from openshelf_os.storage import (
    CalibrationCoordinate,
    Cell,
    CellStatus,
    Dimensions,
    Shelf,
    StorageGrid,
    StorageModuleSide,
)
from openshelf_os_server.core.laser_auto_calibration import (
    LaserAutoCalibrationManager,
    LaserAutoCalibrationScope,
)


MANUAL_X_MM = 354.0
MANUAL_Y_MM = 41.0
DETECTED_TOOL_X_MM = 358.0
DETECTED_TOOL_Y_MM = 44.0
CONFIGURED_OFFSET = (-1.0, 1.0)
DERIVED_OFFSET = (MANUAL_X_MM - DETECTED_TOOL_X_MM, MANUAL_Y_MM - DETECTED_TOOL_Y_MM)

# A second shelf that shares cell_idx 0 with the first: a cell scope that drops the shelf would
# take this shelf's manual point into the derivation alongside the scoped one.
SECOND_SHELF_IDX = 3
SECOND_SHELF_MANUAL_X_MM = 352.9
SECOND_SHELF_MANUAL_Y_MM = 640.2
SECOND_SHELF_DETECTED_TOOL_X_MM = 358.0
SECOND_SHELF_DETECTED_TOOL_Y_MM = 643.4
SECOND_SHELF_DERIVED_OFFSET = (
	SECOND_SHELF_MANUAL_X_MM - SECOND_SHELF_DETECTED_TOOL_X_MM,
	SECOND_SHELF_MANUAL_Y_MM - SECOND_SHELF_DETECTED_TOOL_Y_MM,
)


def _build_cell(*, cell_idx: int, x: float, y: float, is_manual: bool) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=CalibrationCoordinate(x=x, y=y, s=0, z_offset=6.5),
		dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
		is_manual=is_manual,
	)


def _build_source_grid(*, is_manual: bool = True, include_second_shelf: bool = False) -> StorageGrid:
	grid = StorageGrid(filename="storage.json")
	shelves = [
		Shelf(
			shelf_idx=0,
			shelf_height=30.0,
			shelf_width=200.0,
			cells=[
				_build_cell(cell_idx=0, x=MANUAL_X_MM, y=MANUAL_Y_MM, is_manual=is_manual),
				_build_cell(
					cell_idx=1,
					x=DETECTED_TOOL_X_MM,
					y=DETECTED_TOOL_Y_MM,
					is_manual=False,
				),
			],
		)
	]

	if include_second_shelf:
		shelves.append(
			Shelf(
				shelf_idx=SECOND_SHELF_IDX,
				shelf_height=30.0,
				shelf_width=200.0,
				cells=[
					_build_cell(
						cell_idx=0,
						x=SECOND_SHELF_MANUAL_X_MM,
						y=SECOND_SHELF_MANUAL_Y_MM,
						is_manual=is_manual,
					)
				],
			)
		)

	grid.left_module = shelves
	return grid
def _build_analysis(*, include_second_shelf: bool = False) -> SimpleNamespace:
	shelves = [
		ScanDetectedShelf(
			shelf_idx=0,
			center_y_mm=DETECTED_TOOL_Y_MM,
			shelf_height_mm=30.0,
			shelf_width_mm=200.0,
			cells=[
				ScanDetectedCell(
					shelf_idx=0,
					cell_idx=0,
					center_x_mm=DETECTED_TOOL_X_MM - 51.0,
					center_y_mm=DETECTED_TOOL_Y_MM,
					tool_x_mm=DETECTED_TOOL_X_MM,
					tool_y_mm=DETECTED_TOOL_Y_MM,
					width_mm=26.0,
					height_mm=30.0,
					point_count=20,
					bbox_x_min=DETECTED_TOOL_X_MM - 13.0,
					bbox_x_max=DETECTED_TOOL_X_MM + 13.0,
					bbox_y_min=DETECTED_TOOL_Y_MM - 15.0,
					bbox_y_max=DETECTED_TOOL_Y_MM + 15.0,
				)
			],
		)
	]

	if include_second_shelf:
		shelves.append(
			ScanDetectedShelf(
				shelf_idx=SECOND_SHELF_IDX,
				center_y_mm=SECOND_SHELF_DETECTED_TOOL_Y_MM,
				shelf_height_mm=30.0,
				shelf_width_mm=200.0,
				cells=[
					ScanDetectedCell(
						shelf_idx=SECOND_SHELF_IDX,
						cell_idx=0,
						center_x_mm=SECOND_SHELF_DETECTED_TOOL_X_MM - 51.0,
						center_y_mm=SECOND_SHELF_DETECTED_TOOL_Y_MM,
						tool_x_mm=SECOND_SHELF_DETECTED_TOOL_X_MM,
						tool_y_mm=SECOND_SHELF_DETECTED_TOOL_Y_MM,
						width_mm=26.0,
						height_mm=30.0,
						point_count=20,
						bbox_x_min=SECOND_SHELF_DETECTED_TOOL_X_MM - 13.0,
						bbox_x_max=SECOND_SHELF_DETECTED_TOOL_X_MM + 13.0,
						bbox_y_min=SECOND_SHELF_DETECTED_TOOL_Y_MM - 15.0,
						bbox_y_max=SECOND_SHELF_DETECTED_TOOL_Y_MM + 15.0,
					)
			],
			)
		)

	return SimpleNamespace(
		shelves=shelves,
		metadata_path=Path("/tmp/fake-reference-metadata.json"),
	)
def _build_parameters() -> Parameters:
	return Parameters.model_validate(
		{
			"IDLE_POSITION": [0, 0],
			"PICKUP_POSITION_L": [0, 0],
			"PICKUP_POSITION_H": [0, 0],
			"SCAN_POSITION": [0, 0],
			"STAGE_EXTENSION_IO_BOX": 0,
			"STAGE_NUDGE": 0,
			"STAGE_EXTENSION_SHELF_FRONT": 47.0,
			"STAGE_EXTENSION_SHELF_BACK": 46.0,
			"Z_EXTENSION_PICK": 0,
			"Z_EXTENSION_PLACE": 160.5,
			"Z_EXTENSION_IO": 0,
			"Z_MOVE_RPM": 1,
			"Z_MOVE_ACCEL": 1,
			"ROTATION_FRONT": 0,
			"ROTATION_BACK": 180,
		}
	)


def _build_root_config(scan_config: SimpleNamespace) -> SimpleNamespace:
	config = SimpleNamespace(
		robot=SimpleNamespace(
			tool=SimpleNamespace(parameters=_build_parameters()),
			grid=SimpleNamespace(path="storage.json", scan=scan_config),
		)
	)
	config.model_copy = lambda deep=True: copy.deepcopy(config)
	return config


def _build_scan_config(*, xy_offset: tuple[float, float] | None) -> SimpleNamespace:
	return SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		left_xy_offset=None if xy_offset is None else SimpleNamespace(x_mm=xy_offset[0], y_mm=xy_offset[1]),
		right_xy_offset=None,
		left_bounds_x_min_mm=5.0,
		left_bounds_x_max_mm=950.0,
		left_bounds_y_min_mm=0.0,
		left_bounds_y_max_mm=1250.0,
		right_bounds_x_min_mm=5.0,
		right_bounds_x_max_mm=945.0,
		right_bounds_y_min_mm=0.0,
		right_bounds_y_max_mm=1250.0,
		z_offset_model_param_tolerance_mm=0.2,
		left_z_offset_model=None,
		right_z_offset_model=None,
		z_stall_anchor_enabled=False,
		z_stall_manual_agreement_mm=1.0,
	)


@pytest.mark.asyncio
async def test_reference_run_uses_derived_offset_and_persists_it() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="side",
		side=StorageModuleSide.LEFT,
		derive_offsets_from_manual_cells=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	source_grid = _build_source_grid()
	saved_configs: list[object] = []
	saved_grids: list[object] = []

	class FakeStorageGrid:
		def __init__(self, *, left_module, right_module, filename) -> None:
			self.left_module = left_module
			self.right_module = right_module
			self.filename = filename
			saved_grids.append(self)

		def save(self, *, version_id=None) -> None:
			self.version_id = version_id

	worker = SimpleNamespace(_robot=SimpleNamespace(reload_parameters=MagicMock(), _grid=None))
	completed_results = [
		SimpleNamespace(
			side=StorageModuleSide.LEFT,
			angle_result=None,
			analysis=_build_analysis(),
		)
	]

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch(
			"openshelf_os_server.core.laser_auto_calibration.resolve_grid_path",
			return_value="storage.json",
		):
			with patch(
				"openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid",
				side_effect=[source_grid, source_grid],
			):
				with patch.object(
					manager,
					"_resolve_z_delta_model",
					AsyncMock(return_value=(1.0, 0.0)),
				):
					with patch(
						"openshelf_os_server.core.laser_auto_calibration.laser_module.apply_wall_depth_z_offsets"
					):
						with patch(
							"openshelf_os_server.core.laser_auto_calibration.laser_module.StorageGrid",
							FakeStorageGrid,
						):
							with patch(
								"openshelf_os_server.core.laser_auto_calibration.save_root_config",
								side_effect=lambda next_config, version_id=None: saved_configs.append(next_config),
							):
								with patch(
									"openshelf_os_server.core.laser_auto_calibration.clear_config_cache"
								):
									await manager._persist_completed_results(worker, completed_results)

	assert len(saved_configs) == 1
	saved_scan_config = saved_configs[0].robot.grid.scan
	assert saved_scan_config.left_xy_offset.x_mm == pytest.approx(DERIVED_OFFSET[0])
	assert saved_scan_config.left_xy_offset.y_mm == pytest.approx(DERIVED_OFFSET[1])

	# The frame the scan wrote is the manual datum, because the derived offset is what turns
	# the detected opening back into the operator's grab point.
	applied_cell = saved_grids[0].left_module[0].cells[0]
	assert applied_cell.calibration_point.x == pytest.approx(MANUAL_X_MM, abs=0.05)
	assert applied_cell.calibration_point.y == pytest.approx(MANUAL_Y_MM, abs=0.05)
	assert applied_cell.is_manual is True

	results = manager._snapshot.reference_offset_results
	assert len(results) == 1
	assert results[0].side == StorageModuleSide.LEFT
	assert results[0].offset_x_mm == pytest.approx(DERIVED_OFFSET[0])
	assert results[0].offset_y_mm == pytest.approx(DERIVED_OFFSET[1])
	assert results[0].configured_offset_x_mm == pytest.approx(CONFIGURED_OFFSET[0])
	assert results[0].applied is True
	assert results[0].persisted is True
	assert results[0].sample_count == 1
	assert results[0].samples[0].detected_x_mm == pytest.approx(DETECTED_TOOL_X_MM)


@pytest.mark.asyncio
async def test_reference_run_without_a_manual_cell_never_starts() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=_build_source_grid(is_manual=False)))

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with pytest.raises(ValueError, match="marked manual"):
			await manager.ensure_session_started(
				worker,
				LaserAutoCalibrationScope(
					mode="side",
					side=StorageModuleSide.LEFT,
					derive_offsets_from_manual_cells=True,
				),
			)

	assert manager._task is None


@pytest.mark.asyncio
async def test_reference_run_needs_a_manual_cell_on_every_side_in_scope() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	# Both sides carry a configured offset: the derivation needs one as its predictor, and this
	# test is about the manual-cell rule, not about that refusal.
	scan_config.right_xy_offset = SimpleNamespace(x_mm=-97.0, y_mm=0.0)
	config = _build_root_config(scan_config)
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=_build_source_grid()))

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with pytest.raises(ValueError, match="right_module"):
			await manager.ensure_session_started(
				worker,
				LaserAutoCalibrationScope(
					mode="both",
					derive_offsets_from_manual_cells=True,
				),
			)

	assert manager._task is None


@pytest.mark.asyncio
async def test_single_cell_reference_run_keeps_the_datum_and_persists_the_offset() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="cell",
		side=StorageModuleSide.LEFT,
		shelf_idx=0,
		cell_idx=0,
		derive_offsets_from_manual_cells=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	grid = _build_source_grid()
	saved_configs: list[object] = []
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=grid))
	target = SimpleNamespace(shelf_idx=0, cell_idx=0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch(
			"openshelf_os_server.core.laser_auto_calibration.resolve_grid_path",
			return_value="storage.json",
		):
			with patch(
				"openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid",
				return_value=grid,
			):
				with patch(
					"openshelf_os_server.core.laser_auto_calibration.laser_module.find_detected_cell_for_candidate",
					return_value=_build_analysis().shelves[0].cells[0],
				):
					with patch(
						"openshelf_os_server.core.laser_auto_calibration.laser_module.apply_single_cell_rescan"
					) as apply_rescan:
						with patch(
							"openshelf_os_server.core.laser_auto_calibration.save_root_config",
							side_effect=lambda next_config, version_id=None: saved_configs.append(next_config),
						):
							with patch(
								"openshelf_os_server.core.laser_auto_calibration.clear_config_cache"
							):
								await manager._persist_single_cell_result(
									worker,
									StorageModuleSide.LEFT,
									_build_analysis(),
									target,
									MagicMock(),
								)

	# A reference cell is the datum: it must not be rewritten from its own detection.
	apply_rescan.assert_not_called()
	cell_point = grid.left_module[0].cells[0].calibration_point
	assert cell_point.x == pytest.approx(MANUAL_X_MM)
	assert cell_point.y == pytest.approx(MANUAL_Y_MM)

	assert len(saved_configs) == 1
	assert saved_configs[0].robot.grid.scan.left_xy_offset.x_mm == pytest.approx(DERIVED_OFFSET[0])
	assert saved_configs[0].robot.grid.scan.left_xy_offset.y_mm == pytest.approx(DERIVED_OFFSET[1])
	assert manager._snapshot.reference_offset_results[0].applied is True
	assert manager._snapshot.reference_offset_results[0].persisted is True


@pytest.mark.asyncio
async def test_cell_scoped_reference_run_uses_only_the_in_scope_datum() -> None:
	"""A cell scope derives from the (shelf, cell) it targeted, not every shelf holding the cell.

	The pre-motion gate checks the scope as (shelf_idx, cell_idx); a derivation that dropped the
	shelf would median the sibling shelf's manual point into the persisted module offset.
	"""
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="cell",
		side=StorageModuleSide.LEFT,
		shelf_idx=SECOND_SHELF_IDX,
		cell_idx=0,
		derive_offsets_from_manual_cells=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	grid = _build_source_grid(include_second_shelf=True)
	saved_configs: list[object] = []
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=grid))
	target = SimpleNamespace(shelf_idx=SECOND_SHELF_IDX, cell_idx=0)
	analysis = _build_analysis(include_second_shelf=True)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch(
			"openshelf_os_server.core.laser_auto_calibration.resolve_grid_path",
			return_value="storage.json",
		):
			with patch(
				"openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid",
				return_value=grid,
			):
				with patch(
					"openshelf_os_server.core.laser_auto_calibration.laser_module.find_detected_cell_for_candidate",
					return_value=analysis.shelves[0].cells[0],
				):
					with patch(
						"openshelf_os_server.core.laser_auto_calibration.laser_module.apply_single_cell_rescan"
					) as apply_rescan:
						with patch(
							"openshelf_os_server.core.laser_auto_calibration.save_root_config",
							side_effect=lambda next_config, version_id=None: saved_configs.append(next_config),
						):
							with patch(
								"openshelf_os_server.core.laser_auto_calibration.clear_config_cache"
						):
								await manager._persist_single_cell_result(
									worker,
									StorageModuleSide.LEFT,
									analysis,
									target,
									MagicMock(),
								)

	apply_rescan.assert_not_called()
	assert len(saved_configs) == 1
	saved_offset = saved_configs[0].robot.grid.scan.left_xy_offset
	# The in-scope datum alone: the sibling shelf's point sits 1.1mm away, so a derivation that
	# dropped the shelf would have persisted the median of the two datums, a value neither holds.
	assert saved_offset.x_mm == pytest.approx(SECOND_SHELF_DERIVED_OFFSET[0])
	assert saved_offset.y_mm == pytest.approx(SECOND_SHELF_DERIVED_OFFSET[1])
	result = manager._snapshot.reference_offset_results[0]
	assert result.sample_count == 1
	assert result.samples[0].shelf_idx == SECOND_SHELF_IDX
	assert result.applied is True
	assert result.persisted is True


@pytest.mark.asyncio
async def test_shelf_reference_run_frames_the_apply_with_the_derived_offset() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="shelf",
		side=StorageModuleSide.LEFT,
		shelf_idx=0,
		derive_offsets_from_manual_cells=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	grid = _build_source_grid()
	saved_configs: list[object] = []
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=grid))
	applied_scan_configs: list[SimpleNamespace] = []

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch(
			"openshelf_os_server.core.laser_auto_calibration.resolve_grid_path",
			return_value="storage.json",
		):
			with patch(
				"openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid",
				return_value=grid,
			):
				with patch.object(
					manager,
					"_resolve_z_delta_model",
					AsyncMock(return_value=None),
				):
					with patch(
						"openshelf_os_server.core.laser_auto_calibration.laser_module.apply_single_shelf_analysis",
						side_effect=lambda **kwargs: applied_scan_configs.append(kwargs["scan_config"]),
					):
						with patch(
							"openshelf_os_server.core.laser_auto_calibration.save_root_config",
							side_effect=lambda next_config, version_id=None: saved_configs.append(next_config),
						):
							with patch(
								"openshelf_os_server.core.laser_auto_calibration.clear_config_cache"
							):
								await manager._persist_single_shelf_result(
									worker,
									StorageModuleSide.LEFT,
									_build_analysis(),
									0,
								)

	assert len(applied_scan_configs) == 1
	assert applied_scan_configs[0].left_xy_offset.x_mm == pytest.approx(DERIVED_OFFSET[0])
	assert applied_scan_configs[0].left_xy_offset.y_mm == pytest.approx(DERIVED_OFFSET[1])
	# The configured config object itself stays untouched; the derived frame rides the copy.
	assert scan_config.left_xy_offset.x_mm == pytest.approx(CONFIGURED_OFFSET[0])
	assert len(saved_configs) == 1
	assert manager._snapshot.reference_offset_results[0].persisted is True


@pytest.mark.asyncio
async def test_default_run_reports_no_reference_derivation() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.LEFT)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		result = await manager._derive_reference_offset(
			worker=SimpleNamespace(),
			side=StorageModuleSide.LEFT,
			analysis=_build_analysis(),
			source_grid=_build_source_grid(),
		)

	assert result is None
	assert manager._snapshot.reference_offset_results == []


@pytest.mark.asyncio
async def test_unmatched_reference_cell_fails_the_run_instead_of_using_the_config() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="side",
		side=StorageModuleSide.LEFT,
		derive_offsets_from_manual_cells=True,
	)
	manager._reference_offset_entries = []
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	analysis = _build_analysis()
	analysis.shelves[0].cells[0].tool_x_mm = 900.0

	# `apply_runner` binds `get_root_config` through a star import, so the derivation reads the
	# real config unless that module's own name is patched. The test needs its configured offset
	# to reach the derivation: predicting from nothing is refused before matching now.
	with patch(
		"openshelf_os_server.core.laser_auto_calibration.apply_runner.get_root_config",
		return_value=config,
	):
		with pytest.raises(ValueError, match="matched no manual reference cell"):
			await manager._derive_reference_offset(
				worker=SimpleNamespace(),
				side=StorageModuleSide.LEFT,
				analysis=analysis,
				source_grid=_build_source_grid(),
			)


def test_reference_offset_result_describes_its_delta() -> None:
	result = ReferenceOffsetResult(
		side=StorageModuleSide.LEFT,
		offset_x_mm=-4.0,
		offset_y_mm=-3.0,
		spread_x_mm=0.0,
		spread_y_mm=0.0,
		configured_offset_x_mm=-1.0,
		configured_offset_y_mm=1.0,
	)

	assert result.described_delta_mm() == (pytest.approx(-3.0), pytest.approx(-4.0))
	assert ReferenceOffsetResult(
		side=StorageModuleSide.LEFT,
		offset_x_mm=-4.0,
		offset_y_mm=-3.0,
		spread_x_mm=0.0,
		spread_y_mm=0.0,
	).described_delta_mm() == (None, None)


# --------------------------------------------------------------------------- #
#  Probed z model (z stall anchor)
# --------------------------------------------------------------------------- #


MANUAL_Z_OFFSET_MM = 6.5
MANUAL_WALL_DEPTH_MM = 9.62
# What the hand-calibrated cell implies: z_offset = wall_depth + intercept + slope * x.
MANUAL_INTERCEPT_MM = round(MANUAL_Z_OFFSET_MM - MANUAL_WALL_DEPTH_MM, 3)


def _build_manual_reference(
	*,
	intercept_mm: float | None = MANUAL_INTERCEPT_MM,
	captured_stage_mm: float | None = None,
) -> ReferenceOffsetResult:
	from openshelf_os.laser_calibration.reference_offsets import ReferenceOffsetSample

	sample = ReferenceOffsetSample(
		shelf_idx=0,
		cell_idx=0,
		manual_x_mm=MANUAL_X_MM,
		manual_y_mm=MANUAL_Y_MM,
		detected_x_mm=DETECTED_TOOL_X_MM,
		detected_y_mm=DETECTED_TOOL_Y_MM,
		delta_x_mm=MANUAL_X_MM - DETECTED_TOOL_X_MM,
		delta_y_mm=MANUAL_Y_MM - DETECTED_TOOL_Y_MM,
		wall_depth_mm=None if intercept_mm is None else MANUAL_WALL_DEPTH_MM,
		derived_intercept_mm=intercept_mm,
		z_captured_stage_mm=captured_stage_mm,
	)

	return ReferenceOffsetResult(
		side=StorageModuleSide.LEFT,
		offset_x_mm=sample.delta_x_mm,
		offset_y_mm=sample.delta_y_mm,
		spread_x_mm=0.0,
		spread_y_mm=0.0,
		samples=[sample],
		intercept_mm=intercept_mm,
		z_captured_stage_mm=captured_stage_mm,
		z_captured_stage_samples=0 if captured_stage_mm is None else 1,
	)


def _build_z_stall_result(
	*,
	intercept_mm: float = MANUAL_INTERCEPT_MM,
	slope_per_mm: float = 0.0,
	x_span_mm: float = 400.0,
) -> "laser_module.ZStallResult":
	from openshelf_os.laser_calibration.z_stall import ZStallResult, ZStallSample

	# x_span_mm is derived from the samples, so the span under test has to be built into them.
	lo_x_mm = 300.0
	hi_x_mm = lo_x_mm + x_span_mm

	return ZStallResult(
		side=StorageModuleSide.LEFT,
		intercept_mm=intercept_mm,
		slope_per_mm=slope_per_mm,
		fit_source="tilt" if slope_per_mm != 0.0 else "constant",
		stage_extension_mm=47.0,
		z_place_mm=160.5,
		rotation_center_deg=183.0,
		fit_centroid_x_mm=lo_x_mm + x_span_mm / 2.0,
		samples=[
			ZStallSample(
				shelf_idx=0,
				cell_idx=1,
				probe_x_mm=lo_x_mm,
				probe_y_mm=40.0,
				wall_depth_mm=MANUAL_WALL_DEPTH_MM,
				z_contact_mm=170.0,
				z_contact_repeat_mm=170.1,
				delta_mm=intercept_mm,
				reason="stall",
			),
			ZStallSample(
				shelf_idx=0,
				cell_idx=2,
				probe_x_mm=hi_x_mm,
				probe_y_mm=40.0,
				wall_depth_mm=MANUAL_WALL_DEPTH_MM,
				z_contact_mm=170.2,
				z_contact_repeat_mm=170.2,
				delta_mm=intercept_mm,
				reason="stall",
			),
		],
	)


def _build_z_worker(*, grid: StorageGrid | None = None) -> SimpleNamespace:
	motor = SimpleNamespace(_params=SimpleNamespace(lim=950.0, lower_lim=0.0))
	tool = SimpleNamespace(
		get_param=AsyncMock(return_value=None),
		_mx=SimpleNamespace(_params=SimpleNamespace(lim=950.0, lower_lim=0.0)),
		_my=motor,
		_mg=SimpleNamespace(_params=SimpleNamespace(lim=180.0)),
	)
	robot = SimpleNamespace(
		_tool=tool,
		_grid=grid if grid is not None else _build_source_grid(),
		reload_parameters=MagicMock(),
	)

	return SimpleNamespace(_robot=robot)


@pytest.mark.asyncio
async def test_z_stall_replaces_the_configured_model_with_the_probed_one() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=1.052,
		slope_per_mm=-4.5e-05,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	probed = _build_z_stall_result()

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(
			laser_module,
			"run_z_stall_anchor",
			AsyncMock(return_value=probed),
		):
			result = await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	assert result == (pytest.approx(MANUAL_INTERCEPT_MM), pytest.approx(0.0))
	# The probed model is installed on the run's config, which is what persists it.
	installed = scan_config.left_z_offset_model
	assert installed.intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert installed.slope_per_mm == pytest.approx(0.0)
	assert installed.stage_extension_mm == pytest.approx(47.0)
	assert installed.z_place_mm == pytest.approx(160.5)

	entries = manager._snapshot.z_stall_results
	assert len(entries) == 1
	assert entries[0].side == StorageModuleSide.LEFT
	assert entries[0].manual_intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	# The gate's number, computed while the config still held the slope the datum was derived with.
	# The post-install recompute (which re-read the config after the install) reported 0.0 here.
	assert entries[0].manual_agreement_mm == pytest.approx(0.01593, abs=1e-3)
	assert entries[0].manual_agreement_mm != 0.0
	assert entries[0].persisted is True
	assert entries[0].sample_count == 2
	assert entries[0].samples[0].z_contact_repeat_mm == pytest.approx(170.1)


@pytest.mark.asyncio
async def test_z_stall_refuses_a_probe_the_datum_does_not_corroborate() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	probed = _build_z_stall_result(intercept_mm=MANUAL_INTERCEPT_MM - 4.0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			with pytest.raises(ValueError, match="shallower than the"):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					_build_analysis(),
					scan_config,
					source_grid=_build_source_grid(),
					manual_reference=_build_manual_reference(),
				)

	# Nothing was written, and the failed fit is still visible in the snapshot.
	assert scan_config.left_z_offset_model is None
	assert len(manager._snapshot.z_stall_results) == 1
	assert manager._snapshot.z_stall_results[0].persisted is False


@pytest.mark.asyncio
async def test_z_stall_refuses_a_datum_saved_at_another_stage_extension() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	worker = _build_z_worker()

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(
			laser_module,
			"run_z_stall_anchor",
			AsyncMock(return_value=_build_z_stall_result()),
		):
			with pytest.raises(ValueError, match="saved with the stage at"):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					_build_analysis(),
					scan_config,
					source_grid=_build_source_grid(),
					manual_reference=_build_manual_reference(captured_stage_mm=48.0),
				)


@pytest.mark.asyncio
async def test_z_stall_refuses_without_a_manual_datum() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	worker = _build_z_worker()

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(
			laser_module,
			"run_z_stall_anchor",
			AsyncMock(return_value=_build_z_stall_result()),
		):
			with pytest.raises(ValueError, match="no hand-calibrated reference cell"):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					_build_analysis(),
					scan_config,
					source_grid=_build_source_grid(),
					manual_reference=None,
				)

	# The probe has already moved, so the raw fit must still reach the operator, unjudged.
	assert len(manager._snapshot.z_stall_results) == 1
	entry = manager._snapshot.z_stall_results[0]
	assert entry.side == StorageModuleSide.LEFT
	assert entry.intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert entry.manual_intercept_mm is None
	assert entry.manual_agreement_mm is None
	assert entry.applied is False
	assert entry.persisted is False


@pytest.mark.asyncio
async def test_z_stall_refuses_a_datum_without_a_wall_depth() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	worker = _build_z_worker()

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(
			laser_module,
			"run_z_stall_anchor",
			AsyncMock(return_value=_build_z_stall_result()),
		):
			with pytest.raises(ValueError, match="no hand-calibrated reference cell"):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					_build_analysis(),
					scan_config,
					source_grid=_build_source_grid(),
					manual_reference=_build_manual_reference(intercept_mm=None),
				)

	# The datum exists but carries no wall depth: the raw fit is still published, unjudged.
	assert len(manager._snapshot.z_stall_results) == 1
	entry = manager._snapshot.z_stall_results[0]
	assert entry.side == StorageModuleSide.LEFT
	assert entry.intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert entry.manual_intercept_mm is None
	assert entry.manual_agreement_mm is None
	assert entry.applied is False
	assert entry.persisted is False


@pytest.mark.asyncio
async def test_z_stall_keeps_the_configured_slope_when_the_probe_could_not_span() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=1.052,
		slope_per_mm=-0.00285,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	# A short span: the tilt-free fit reports the deltas' value at the samples' centroid, so
	# substituting the configured slope has to re-base the intercept to x = 0 first.
	probed = _build_z_stall_result(slope_per_mm=0.0, x_span_mm=100.0)
	centroid_x_mm = 300.0 + 100.0 / 2.0
	rebased_intercept_mm = MANUAL_INTERCEPT_MM + 0.00285 * centroid_x_mm

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			result = await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	assert result == (pytest.approx(rebased_intercept_mm), pytest.approx(-0.00285))
	assert scan_config.left_z_offset_model.intercept_mm == pytest.approx(rebased_intercept_mm)
	assert scan_config.left_z_offset_model.slope_per_mm == pytest.approx(-0.00285)
	assert manager._snapshot.z_stall_results[0].slope_source == "config_short_span"


@pytest.mark.asyncio
async def test_z_stall_short_span_substitution_reproduces_the_probe_contacts() -> None:
	"""A substituted slope must not shift the model: the installed line has to pass through the probes.

	The fit is tilt-free, so its intercept is the deltas' value at the samples' centroid. With
	`I_true = -3.0` and `S = 0.002382` the probe measures `I_true + S*x` at x = 300 and 400; the
	installed model has to reproduce both, which only happens once the intercept is re-based.
	"""
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=-3.0,
		slope_per_mm=0.002382,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	probed = _build_z_stall_result(slope_per_mm=0.0, x_span_mm=100.0, intercept_mm=-2.1663)
	datum = _build_manual_reference(intercept_mm=-3.0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			intercept_mm, slope_per_mm = await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=datum,
			)

	installed = scan_config.left_z_offset_model

	assert slope_per_mm == pytest.approx(0.002382)
	assert installed.slope_per_mm == pytest.approx(0.002382)
	# The probe's own reading at x = 350 (the centroid of 300 and 400).
	assert installed.intercept_mm + installed.slope_per_mm * 350.0 == pytest.approx(-2.1663, abs=1e-6)
	# And at the sample ends, where a missing re-base would show up as slope * centroid.
	assert installed.intercept_mm + installed.slope_per_mm * 300.0 == pytest.approx(-2.2854, abs=1e-3)
	assert installed.intercept_mm + installed.slope_per_mm * 400.0 == pytest.approx(-2.0472, abs=1e-3)
	assert intercept_mm == pytest.approx(installed.intercept_mm)


@pytest.mark.asyncio
async def test_z_stall_takes_a_probed_slope_when_it_measured_one() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	# The configured tilt is close enough that the datum corroborates the probe's own: the datum
	# intercept was derived with the configured slope, so a large swing between the two would move
	# the installed model away from the datum cell by more than the agreement tolerance.
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=1.052,
		slope_per_mm=0.0022,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	probed = _build_z_stall_result(slope_per_mm=0.002382, x_span_mm=500.0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			result = await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	assert result == (pytest.approx(MANUAL_INTERCEPT_MM), pytest.approx(0.002382))
	assert manager._snapshot.z_stall_results[0].slope_source == "probe"


@pytest.mark.asyncio
async def test_shelf_scoped_probe_excludes_the_datum_in_the_analysis_key_space() -> None:
	"""A single-shelf scan re-indexes its shelf to 0, so the exclusion has to be translated.

	The stored grid says the datum is `(3, 0)` while the analysis holds that same opening as
	`(0, 0)`. Passing the grid keys through unchanged excluded nothing, so the probe pressed the
	datum and the agreement was judged against a point the fit was taken at.
	"""
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="shelf",
		side=StorageModuleSide.LEFT,
		shelf_idx=3,
		derive_z_model_from_stall=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	# The datum is stored on shelf 3, while the shelf-scoped analysis reports shelf 0.
	grid = _build_source_grid(include_second_shelf=True)
	worker = _build_z_worker(grid=grid)
	analysis = _build_analysis()

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.apply_runner.get_root_config",
		return_value=config,
	):
		with patch.object(
			laser_module,
			"run_z_stall_anchor",
			AsyncMock(return_value=_build_z_stall_result()),
		) as anchor:
			with patch.object(laser_module, "excluded_probe_cells", return_value={(3, 0)}):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					analysis,
					scan_config,
					source_grid=grid,
					manual_reference=_build_manual_reference(),
				)

	assert anchor.await_args is not None
	assert anchor.await_args.kwargs["excluded_cells"] == {(0, 0)}


@pytest.mark.asyncio
async def test_z_stall_refuses_a_probed_slope_the_datum_cannot_corroborate() -> None:
	"""Intercepts that agree are not enough: the datum speaks for the model that is installed.

	The datum intercept carries the configured slope, so a probe-fitted tilt that differs from it
	tilts the installed model away from the datum cell by `(S_probe - S_cfg) * x`. Here both
	intercepts are identical and the model still misses the datum by 0.005 * MANUAL_X_MM.
	"""
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=MANUAL_INTERCEPT_MM,
		slope_per_mm=0.0,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	probed = _build_z_stall_result(intercept_mm=MANUAL_INTERCEPT_MM, slope_per_mm=0.005, x_span_mm=500.0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			with pytest.raises(ValueError, match="tolerance"):
				await manager._resolve_z_delta_model(
					worker,
					StorageModuleSide.LEFT,
					_build_analysis(),
					scan_config,
					source_grid=_build_source_grid(),
					manual_reference=_build_manual_reference(),
				)

	published = manager._snapshot.z_stall_results[0]

	assert published.manual_agreement_mm == pytest.approx(0.005 * MANUAL_X_MM)
	assert published.applied is False
	assert published.persisted is False


@pytest.mark.asyncio
async def test_z_stall_probes_never_include_the_manual_datum() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	config = _build_root_config(scan_config)
	grid = _build_source_grid()
	# A production tray home: the probe may press it, because a tray offers the same divider to the
	# laser and the same gripper interface as an empty cell.
	grid.left_module[0].cells[1].status = CellStatus.MAINTENANCE
	worker = _build_z_worker(grid=grid)
	anchor = AsyncMock(return_value=_build_z_stall_result())
	# The scope detected the tray-home opening too: an independent opening in scope is what
	# holds the datum out of the sample set, so the probe must still never take it.
	analysis = _build_analysis()
	analysis.shelves[0].cells.append(
		ScanDetectedCell(
			shelf_idx=0,
			cell_idx=1,
			center_x_mm=DETECTED_TOOL_X_MM + 54.0,
			center_y_mm=DETECTED_TOOL_Y_MM,
			tool_x_mm=DETECTED_TOOL_X_MM + 105.0,
			tool_y_mm=DETECTED_TOOL_Y_MM,
			width_mm=26.0,
			height_mm=30.0,
			point_count=20,
			bbox_x_min=DETECTED_TOOL_X_MM + 92.0,
			bbox_x_max=DETECTED_TOOL_X_MM + 118.0,
			bbox_y_min=DETECTED_TOOL_Y_MM - 15.0,
			bbox_y_max=DETECTED_TOOL_Y_MM + 15.0,
		)
	)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", anchor):
			await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				analysis,
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	assert anchor.await_args is not None
	# The hand-calibrated datum stays out of the sample set so the agreement check stays honest,
	# and the tray home is not in the exclusion set at all.
	assert anchor.await_args.kwargs["excluded_cells"] == {(0, 0)}
	assert anchor.await_args.kwargs["z_axis_limit_mm"] == pytest.approx(180.0)
	assert anchor.await_args.kwargs["xy_offset"] == CONFIGURED_OFFSET


@pytest.mark.asyncio
async def test_scope_flag_requests_the_probe_without_touching_the_config() -> None:
	"""The api caller can ask for a probed model; the machine setting stays untouched."""
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	anchor = AsyncMock(return_value=_build_z_stall_result())
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="shelf",
		side=StorageModuleSide.LEFT,
		shelf_idx=0,
		derive_z_model_from_stall=True,
	)

	assert scan_config.z_stall_anchor_enabled is False

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", anchor):
			await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				allow_missing=True,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	anchor.assert_awaited_once()
	assert scan_config.left_z_offset_model.intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert manager._snapshot.z_stall_results[0].persisted is True


@pytest.mark.asyncio
async def test_reference_run_without_the_probe_flag_never_probes() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="shelf",
		side=StorageModuleSide.LEFT,
		shelf_idx=0,
		derive_offsets_from_manual_cells=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	worker = _build_z_worker()
	anchor = AsyncMock(return_value=_build_z_stall_result())

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch.object(laser_module, "run_z_stall_anchor", anchor):
			result = await manager._resolve_z_delta_model(
				worker,
				StorageModuleSide.LEFT,
				_build_analysis(),
				scan_config,
				allow_missing=True,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(),
			)

	anchor.assert_not_awaited()
	assert result is None


@pytest.mark.asyncio
async def test_cell_scoped_probe_verifies_the_datum_and_persists_the_model() -> None:
	"""A run scoped to the reference cell is the cheapest way to check its z by contact."""
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="cell",
		side=StorageModuleSide.LEFT,
		shelf_idx=0,
		cell_idx=0,
		derive_z_model_from_stall=True,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.left_z_offset_model = SimpleNamespace(
		intercept_mm=1.052,
		slope_per_mm=-0.00285,
		stage_extension_mm=47.0,
		z_place_mm=160.5,
	)
	config = _build_root_config(scan_config)
	grid = _build_source_grid()
	saved_configs: list[object] = []
	worker = _build_z_worker(grid=grid)
	# One opening is one probe point, so there is no x span to fit a tilt from: the fit reports the
	# datum's line at the fit centre, and re-basing recovers the datum's intercept from it.
	centroid_x_mm = 300.0
	anchor = AsyncMock(
		return_value=_build_z_stall_result(
			slope_per_mm=0.0,
			x_span_mm=0.0,
			intercept_mm=MANUAL_INTERCEPT_MM + (-0.00285) * centroid_x_mm,
		)
	)
	target = SimpleNamespace(shelf_idx=0, cell_idx=0)

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with patch(
			"openshelf_os_server.core.laser_auto_calibration.resolve_grid_path",
			return_value="storage.json",
		):
			with patch(
				"openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid",
				return_value=grid,
			):
				with patch(
					"openshelf_os_server.core.laser_auto_calibration.laser_module.find_detected_cell_for_candidate",
					return_value=_build_analysis().shelves[0].cells[0],
				):
					with patch(
						"openshelf_os_server.core.laser_auto_calibration.laser_module.apply_single_cell_rescan"
					):
						with patch.object(
							laser_module,
							"derive_reference_offsets",
							MagicMock(return_value=_build_manual_reference()),
						):
							with patch.object(laser_module, "run_z_stall_anchor", anchor):
								with patch(
									"openshelf_os_server.core.laser_auto_calibration.save_root_config",
									side_effect=lambda next_config, version_id=None: saved_configs.append(
										next_config
									),
								):
									with patch(
										"openshelf_os_server.core.laser_auto_calibration.clear_config_cache"
									):
										await manager._persist_single_cell_result(
											worker,
											StorageModuleSide.LEFT,
											_build_analysis(),
											target,
											MagicMock(),
										)

	anchor.assert_awaited_once()
	# The scope holds nothing but the datum, so the datum must stay out of the exclusion set: a
	# probe that excluded it would have nothing to press and would fail after the scan already ran.
	assert (0, 0) not in anchor.await_args.kwargs["excluded_cells"]
	assert len(saved_configs) == 1
	# The intercept comes from the probe; the span is one cell, so the machine keeps its slope.
	installed = saved_configs[0].robot.grid.scan.left_z_offset_model
	assert installed.intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert installed.slope_per_mm == pytest.approx(-0.00285)
	assert manager._snapshot.z_stall_results[0].slope_source == "config_short_span"
	# The probed cell is the datum: it must not be rewritten from its own detection.
	assert grid.left_module[0].cells[0].calibration_point.z_offset == pytest.approx(MANUAL_Z_OFFSET_MM)
