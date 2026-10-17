"""The reference-frame derivation path of the automatic laser calibration."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os import laser_calibration as laser_module
from openshelf_os.common import Parameters
from openshelf_os.laser_calibration.reference_offsets import ReferenceOffsetResult, ReferenceOffsetSample
from openshelf_os.laser_calibration.scan import ScanDetectedCell, ScanDetectedShelf
from openshelf_os.laser_calibration.z_stall import ZStallResult, ZStallSample
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
    _sync_patch_targets,
)


MANUAL_X_MM = 354.0
MANUAL_Y_MM = 41.0
DETECTED_TOOL_X_MM = 358.0
DETECTED_TOOL_Y_MM = 44.0
CONFIGURED_OFFSET = (-1.0, 1.0)
DERIVED_OFFSET = (MANUAL_X_MM - DETECTED_TOOL_X_MM, MANUAL_Y_MM - DETECTED_TOOL_Y_MM)

# A second shelf that shares cell_idx 0 with the first: a cell scope that dropped the shelf would
# take this shelf's manual point into the derivation alongside the datum.
SECOND_SHELF_IDX = 3
SECOND_SHELF_MANUAL_X_MM = 352.9
SECOND_SHELF_MANUAL_Y_MM = 640.2
SECOND_SHELF_DETECTED_TOOL_X_MM = 358.0
SECOND_SHELF_DETECTED_TOOL_Y_MM = 643.4


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

	grid.front_module = shelves
	return grid
def _build_analysis(*, include_second_shelf: bool = False) -> SimpleNamespace:
	shelves = [
		ScanDetectedShelf(
			shelf_idx=0,
			center_y_mm=DETECTED_TOOL_Y_MM,
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
			"PICKUP_POSITION_MULTI_ITEM": [0, 0],
			"SCAN_POSITION": [0, 0],
			"STAGE_EXTENSION_IO_BOX": 0,
			"STAGE_EXTENSION_IO_BOX_MULTI_ITEM": 0,
			"STAGE_NUDGE": 0,
			"STAGE_EXTENSION_SHELF_FRONT": 47.0,
			"STAGE_EXTENSION_SHELF_BACK": 46.0,
			"Z_EXTENSION_PICK": 0,
			"Z_EXTENSION_PLACE": 160.5,
			"Z_EXTENSION_IO": 0,
			"Z_EXTENSION_IO_MULTI_ITEM": 0,
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
		front_gripper_xy_correction_mm=None if xy_offset is None else SimpleNamespace(x_mm=xy_offset[0], y_mm=xy_offset[1]),
		back_gripper_xy_correction_mm=None,
		front_bounds_x_min_mm=5.0,
		front_bounds_x_max_mm=950.0,
		front_bounds_y_min_mm=0.0,
		front_bounds_y_max_mm=1250.0,
		back_bounds_x_min_mm=5.0,
		back_bounds_x_max_mm=945.0,
		back_bounds_y_min_mm=0.0,
		back_bounds_y_max_mm=1250.0,
		z_offset_model_param_tolerance_mm=0.2,
		front_wall_depth_to_gripper_z_model=None,
		back_wall_depth_to_gripper_z_model=None,
		z_stall_anchor_enabled=False,
		z_stall_manual_agreement_mm=1.0,
	)


@pytest.mark.asyncio
async def test_start_without_a_manual_datum_cell_never_starts() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=_build_source_grid(is_manual=False)))

	with patch(
		"openshelf_os_server.core.laser_auto_calibration.get_root_config",
		return_value=config,
	):
		with pytest.raises(ValueError, match="bottom-left cell"):
			await manager.ensure_session_started(
				worker,
				LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.FRONT),
			)

	assert manager._task is None


@pytest.mark.asyncio
async def test_cell_scoped_run_on_the_datum_derives_and_persists_the_offset() -> None:
	"""The datum's own cell is the narrow scope that does re-derive the frame."""
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="cell",
		side=StorageModuleSide.FRONT,
		shelf_idx=0,
		cell_idx=0,
	)
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	config = _build_root_config(scan_config)
	grid = _build_source_grid(include_second_shelf=True)
	saved_configs: list[object] = []
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=grid))
	target = SimpleNamespace(shelf_idx=0, cell_idx=0)
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
									StorageModuleSide.FRONT,
									analysis,
									target,
									MagicMock(),
								)

	apply_rescan.assert_not_called()
	assert len(saved_configs) == 1
	saved_offset = saved_configs[0].robot.grid.scan.front_gripper_xy_correction_mm
	assert saved_offset.x_mm == pytest.approx(DERIVED_OFFSET[0])
	assert saved_offset.y_mm == pytest.approx(DERIVED_OFFSET[1])
	result = manager._snapshot.reference_offset_results[0]
	assert result.sample_count == 1
	assert result.samples[0].shelf_idx == 0
	assert result.applied is True
	assert result.persisted is True


@pytest.mark.asyncio
async def test_unmatched_reference_cell_fails_the_run_instead_of_using_the_config() -> None:
	manager = LaserAutoCalibrationManager()
	manager._snapshot.requested_scope = LaserAutoCalibrationScope(
		mode="side",
		side=StorageModuleSide.FRONT,
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
		with pytest.raises(ValueError, match="no manual reference cell matched"):
			await manager._derive_reference_offset(
				side=StorageModuleSide.FRONT,
				analysis=analysis,
				source_grid=_build_source_grid(),
			)


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
		side=StorageModuleSide.FRONT,
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
	# x_span_mm is derived from the samples, so the span under test has to be built into them.
	lo_x_mm = 300.0
	hi_x_mm = lo_x_mm + x_span_mm

	return ZStallResult(
		side=StorageModuleSide.FRONT,
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


def _per_shelf_probe(*, datum_delta_mm: float, upper_delta_mm: float) -> "laser_module.ZStallResult":
	def sample(shelf_idx: int, probe_y_mm: float, delta_mm: float) -> ZStallSample:
		return ZStallSample(
			shelf_idx=shelf_idx,
			cell_idx=0,
			probe_x_mm=DETECTED_TOOL_X_MM + 20.0,
			probe_y_mm=probe_y_mm,
			wall_depth_mm=MANUAL_WALL_DEPTH_MM,
			z_contact_mm=168.0,
			z_contact_repeat_mm=168.1,
			delta_mm=delta_mm,
			reason="alarm",
		)

	return ZStallResult(
		side=StorageModuleSide.FRONT,
		intercept_mm=datum_delta_mm,
		slope_per_mm=0.0,
		fit_source="constant",
		stage_extension_mm=47.0,
		z_place_mm=160.5,
		rotation_center_deg=183.0,
		samples=[
			sample(0, MANUAL_Y_MM, datum_delta_mm),
			sample(SECOND_SHELF_IDX, SECOND_SHELF_MANUAL_Y_MM, upper_delta_mm),
		],
	)


async def _resolve_probed(manager, scan_config, probed):
	config = _build_root_config(scan_config)

	with patch("openshelf_os_server.core.laser_auto_calibration.get_root_config", return_value=config):
		with patch.object(laser_module, "run_z_stall_anchor", AsyncMock(return_value=probed)):
			return await manager._resolve_z_delta_model(
				_build_z_worker(),
				StorageModuleSide.FRONT,
				_build_analysis(include_second_shelf=True),
				scan_config,
				source_grid=_build_source_grid(),
				manual_reference=_build_manual_reference(captured_stage_mm=47.0),
			)


@pytest.mark.asyncio
async def test_z_stall_gives_every_shelf_its_own_intercept_zeroed_on_the_datum() -> None:
	"""lb4a-3: the upper shelves met the gripper ~1.8mm earlier than the floor datum, unseen by the laser."""
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.z_stall_probe_x_shift_mm = 20.0
	# The probe reads 0.3mm deep everywhere; the datum's own probe takes that out of every shelf.
	probed = _per_shelf_probe(datum_delta_mm=MANUAL_INTERCEPT_MM - 0.3, upper_delta_mm=MANUAL_INTERCEPT_MM - 2.1)

	intercept_mm, _, shelf_intercepts = await _resolve_probed(manager, scan_config, probed)

	assert intercept_mm == pytest.approx(MANUAL_INTERCEPT_MM)
	assert sorted(shelf_intercepts) == [
		(pytest.approx(MANUAL_Y_MM), pytest.approx(MANUAL_INTERCEPT_MM)),
		(pytest.approx(SECOND_SHELF_MANUAL_Y_MM), pytest.approx(MANUAL_INTERCEPT_MM - 1.8)),
	]
	installed = scan_config.front_wall_depth_to_gripper_z_model
	assert [(entry.y_mm, entry.intercept_mm) for entry in installed.shelf_intercepts] == [
		(pytest.approx(MANUAL_Y_MM), pytest.approx(MANUAL_INTERCEPT_MM)),
		(pytest.approx(SECOND_SHELF_MANUAL_Y_MM), pytest.approx(MANUAL_INTERCEPT_MM - 1.8)),
	]
	assert laser_module.z_intercept_for_y(
		intercept_mm=intercept_mm, shelf_intercepts=shelf_intercepts, y_mm=SECOND_SHELF_MANUAL_Y_MM + 3.0
	) == pytest.approx(MANUAL_INTERCEPT_MM - 1.8)


@pytest.mark.asyncio
async def test_z_stall_refuses_a_probe_that_disagrees_with_the_datum_it_was_pressed_beside() -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.z_stall_anchor_enabled = True
	scan_config.z_stall_probe_x_shift_mm = 20.0
	probed = _per_shelf_probe(datum_delta_mm=MANUAL_INTERCEPT_MM - 1.6, upper_delta_mm=MANUAL_INTERCEPT_MM - 1.6)

	with pytest.raises(ValueError, match="probe beside the hand-calibrated datum implies an intercept 1.60mm"):
		await _resolve_probed(manager, scan_config, probed)

	# Nothing was installed, and the measurement still reached the operator.
	assert scan_config.front_wall_depth_to_gripper_z_model is None
	assert manager._snapshot.z_stall_results[0].persisted is False


def _configured_model(*, stage_mm: float = 47.0, z_place_mm: float = 160.5) -> SimpleNamespace:
	return SimpleNamespace(
		intercept_mm=1.052,
		x_slope_mm_per_mm=-4.5e-05,
		reference_stage_mm=stage_mm,
		reference_z_place_mm=z_place_mm,
	)


def _stamp_datum(grid: StorageGrid, stage_mm: float | None) -> StorageGrid:
	grid.front_module[0].cells[0].calibration_point.z_captured_at_stage_mm = stage_mm
	return grid


@pytest.mark.parametrize(
	("model", "stamp_stage_mm", "z_stall", "error"),
	[
		pytest.param(None, None, False, "no z source", id="no_source"),
		pytest.param(_configured_model(z_place_mm=158.0), None, False, "Z_EXTENSION_PLACE", id="place_moved"),
		pytest.param(_configured_model(stage_mm=48.0), None, False, None, id="stage_drift_is_carried"),
		pytest.param(None, 46.0, False, None, id="stamped_datum"),
		pytest.param(None, None, True, None, id="probe"),
		# The apply refuses a stamp this far from the configured 47mm stage, so the start does too,
		# even with a configured model, instead of failing after the full scan.
		pytest.param(
			_configured_model(), 30.0, False, r"saved at stage extension 30\.00mm.*live stage extension is 47\.00mm",
			id="stamp_too_far_from_stage",
		),
		# The probe anchors to the same datum, so the apply refuses that stamp after the probe too.
		pytest.param(None, 30.0, True, "saved at stage extension 30", id="stamp_too_far_with_probe"),
	],
)
def test_start_refuses_a_side_whose_z_cannot_resolve(model, stamp_stage_mm, z_stall, error) -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.front_wall_depth_to_gripper_z_model = model
	scan_config.z_stall_anchor_enabled = z_stall
	config = _build_root_config(scan_config)
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=_stamp_datum(_build_source_grid(), stamp_stage_mm)))
	scope = LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.FRONT)

	with patch("openshelf_os_server.core.laser_auto_calibration.get_root_config", return_value=config):
		_sync_patch_targets()

		if error is None:
			manager._validate_z_offset_sources(scope, worker)
		else:
			with pytest.raises(ValueError, match=error) as excinfo:
				manager._validate_z_offset_sources(scope, worker)

			# Enabling the probe cannot fix a stamp it would refuse as well.
			assert ("z stall probe" in str(excinfo.value)) is (stamp_stage_mm is None)


@pytest.mark.parametrize(
	("scope", "z_stall", "refused"),
	[
		pytest.param(
			LaserAutoCalibrationScope(mode="shelf", side=StorageModuleSide.FRONT, shelf_idx=0),
			False,
			True,
			id="datum_shelf",
		),
		pytest.param(
			LaserAutoCalibrationScope(mode="cell", side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0),
			True,
			True,
			id="datum_cell_with_probe",
		),
		pytest.param(
			LaserAutoCalibrationScope(mode="shelf", side=StorageModuleSide.FRONT, shelf_idx=3),
			False,
			False,
			id="shelf_without_datum",
		),
	],
)
def test_start_refuses_a_datum_rescan_whose_stamp_cannot_be_carried(scope, z_stall, refused) -> None:
	manager = LaserAutoCalibrationManager()
	scan_config = _build_scan_config(xy_offset=CONFIGURED_OFFSET)
	scan_config.front_wall_depth_to_gripper_z_model = _configured_model()
	scan_config.z_stall_anchor_enabled = z_stall
	config = _build_root_config(scan_config)
	worker = SimpleNamespace(_robot=SimpleNamespace(_grid=_stamp_datum(_build_source_grid(), 30.0)))

	with patch("openshelf_os_server.core.laser_auto_calibration.get_root_config", return_value=config):
		_sync_patch_targets()

		if refused:
			with pytest.raises(ValueError, match=r"saved at stage extension 30\.00mm"):
				manager._validate_z_offset_sources(scope, worker)
		else:
			manager._validate_z_offset_sources(scope, worker)
