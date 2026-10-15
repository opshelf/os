from __future__ import annotations

from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.reference_offsets import (
	REFERENCE_DATUM_CELL_IDX,
	REFERENCE_DATUM_SHELF_IDX,
	check_datum_stage_carry,
	check_stamped_reference_carry,
	derive_reference_offsets,
	select_manual_reference_cells,
	select_reference_datum_cell,
	stamped_datum_intercept_mm,
)
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
from openshelf_os.config.laser_scan import LaserScanConfig, LaserScanXYOffsetConfig


def build_cell(
	*,
	cell_idx: int,
	x: float,
	y: float,
	z_offset: float = 6.5,
	is_manual: bool = True,
) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=CalibrationCoordinate(x=x, y=y, s=0, z_offset=z_offset),
		dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
		is_manual=is_manual,
	)


def build_grid(cells: list[Cell]) -> StorageGrid:
	grid = StorageGrid(filename="storage.json")
	grid.front_module = [Shelf(shelf_idx=0, cells=cells)]
	return grid


def build_detected_cell(*, cell_idx: int, tool_x: float, tool_y: float) -> ScanDetectedCell:
	return ScanDetectedCell(
		shelf_idx=0,
		cell_idx=cell_idx,
		center_x_mm=tool_x - 51.0,
		center_y_mm=tool_y,
		tool_x_mm=tool_x,
		tool_y_mm=tool_y,
		width_mm=26.0,
		height_mm=30.0,
		point_count=20,
		bbox_x_min=tool_x - 64.0,
		bbox_x_max=tool_x - 38.0,
		bbox_y_min=tool_y - 15.0,
		bbox_y_max=tool_y + 15.0,
	)


def build_analysis(detected_cells: list[ScanDetectedCell]) -> SimpleNamespace:
	return SimpleNamespace(
		shelves=[
			ScanDetectedShelf(
				shelf_idx=0,
				center_y_mm=detected_cells[0].tool_y_mm,
				shelf_width_mm=200.0,
				cells=list(detected_cells),
			)
		],
		metadata_path=None,
	)


def build_scan_config(xy_offset: tuple[float, float] | None = None) -> LaserScanConfig:
	config = LaserScanConfig()
	config.front_gripper_xy_correction_mm = None if xy_offset is None else LaserScanXYOffsetConfig(
		x_mm=xy_offset[0], y_mm=xy_offset[1]
	)
	return config


def derive(grid: StorageGrid, analysis: SimpleNamespace, scan_config: LaserScanConfig):
	return derive_reference_offsets(
		grid=grid,
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=scan_config,
		candidates=[],
	)


def test_derives_offset_from_a_single_manual_cell() -> None:
	# Manual grab point sits 4mm left and 3mm above the detected opening.
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)])

	result = derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))

	assert result.offset_x_mm == pytest.approx(-4.0)
	assert result.offset_y_mm == pytest.approx(-3.0)
	assert result.spread_x_mm == pytest.approx(0.0)
	assert result.spread_y_mm == pytest.approx(0.0)
	assert result.sample_count == 1
	assert result.samples[0].detected_x_mm == pytest.approx(358.0)
	assert result.configured_offset_x_mm == pytest.approx(-1.0)
	assert result.described_delta_mm() == (pytest.approx(-3.0), pytest.approx(-4.0))


def test_median_across_reference_cells_absorbs_one_noisy_datum() -> None:
	grid = build_grid(
		[
			build_cell(cell_idx=0, x=354.0, y=41.0),
			build_cell(cell_idx=1, x=519.0, y=40.8),
			build_cell(cell_idx=2, x=684.1, y=40.9),
		]
	)
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=523.4, tool_y=44.2),
			build_detected_cell(cell_idx=2, tool_x=688.0, tool_y=44.1),
		]
	)

	result = derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))

	assert result.sample_count == 3
	assert result.offset_x_mm == pytest.approx(-4.0, abs=0.05)
	assert result.offset_y_mm == pytest.approx(-3.2, abs=0.05)
	assert result.spread_x_mm <= 0.5
	assert result.spread_y_mm <= 0.5


def test_large_configured_offset_still_matches_the_reference_cell() -> None:
	"""A ~-100mm offset is larger than the opening pitch, so proximity alone would misfire.

	The neighbouring opening sits closer to the manual point than the reference cell's own
	opening does, which is exactly the case a raw nearest-neighbour match gets wrong.
	"""
	grid = build_grid([build_cell(cell_idx=0, x=250.0, y=41.0)])
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=348.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=210.0, tool_y=44.0),
		]
	)

	result = derive(grid, analysis, build_scan_config(xy_offset=(-97.0, 1.0)))

	assert result.offset_x_mm == pytest.approx(-98.0)
	assert result.offset_y_mm == pytest.approx(-3.0)
	assert result.samples[0].detected_x_mm == pytest.approx(348.0)


def test_reference_datum_needs_the_bottom_left_cell() -> None:
	"""Only a hand-calibrated 0:0 is a datum: any other manual cell is an extra sample."""
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])

	datum = select_reference_datum_cell(grid=grid, side=StorageModuleSide.FRONT)

	assert datum is not None
	assert datum[0] == REFERENCE_DATUM_SHELF_IDX
	assert datum[1].cell_idx == REFERENCE_DATUM_CELL_IDX

	assert select_reference_datum_cell(
		grid=build_grid([build_cell(cell_idx=0, x=354.0, y=41.0, is_manual=False)]),
		side=StorageModuleSide.FRONT,
	) is None
	assert select_reference_datum_cell(
		grid=build_grid([build_cell(cell_idx=1, x=519.0, y=41.0)]),
		side=StorageModuleSide.FRONT,
	) is None

	uncalibrated = build_cell(cell_idx=0, x=354.0, y=41.0)
	uncalibrated.calibration_point = None
	assert select_reference_datum_cell(
		grid=build_grid([uncalibrated]),
		side=StorageModuleSide.FRONT,
	) is None

	elsewhere = StorageGrid(filename="storage.json")
	elsewhere.front_module = [
		Shelf(shelf_idx=5, cells=[build_cell(cell_idx=0, x=354.0, y=641.0)])
	]
	assert select_reference_datum_cell(grid=elsewhere, side=StorageModuleSide.FRONT) is None


def test_refuses_when_no_cell_is_marked_manual() -> None:
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0, is_manual=False)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)])

	with pytest.raises(ValueError, match="marked manual"):
		derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))

	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.FRONT) == []


def test_refuses_when_the_reference_cell_matches_nothing() -> None:
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=520.0, tool_y=44.0)])

	with pytest.raises(ValueError, match="no manual reference cell matched"):
		derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))


def test_refuses_when_reference_cells_disagree() -> None:
	grid = build_grid(
		[
			build_cell(cell_idx=0, x=354.0, y=41.0),
			build_cell(cell_idx=1, x=500.0, y=41.0),
		]
	)
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=523.4, tool_y=44.0),
		]
	)

	with pytest.raises(ValueError, match="disagree"):
		derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))


def test_refuses_to_predict_from_zero_when_no_offset_is_configured() -> None:
	"""With no configured offset the old code predicted from zero and matched by proximity.

	The manual point's own opening sits a full ~105mm pitch away (the true offset is near
	-98), but the neighbouring opening is only ~7mm from the manual point itself: a zero
	prediction would match the neighbour and persist +7 instead of -98, so the derivation
	refuses instead.
	"""
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=452.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=347.0, tool_y=44.0),
		]
	)

	with pytest.raises(
		ValueError,
		match=r"no robot\.grid\.scan\.front_gripper_xy_correction_mm is configured.*set a rough one first",
	):
		derive(grid, analysis, build_scan_config(xy_offset=None))




def test_stamped_datums_are_carried_to_the_live_stage_before_their_intercept() -> None:
	# lb4a-3 front, 2026-10-15: datums jogged at 47.875 and 45.915 describe one tool once both
	# are carried to the 46mm the apply places at.
	cells = [
		build_cell(cell_idx=0, x=354.0, y=41.0, z_offset=10.0),
		build_cell(cell_idx=1, x=459.0, y=41.0, z_offset=12.0),
	]
	cells[0].calibration_point.z_captured_at_stage_mm = 47.875
	cells[1].calibration_point.z_captured_at_stage_mm = 45.915
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=463.0, tool_y=44.0),
		]
	)
	analysis.wall_depths_by_cell = {(0, 0): 9.6, (0, 1): 9.8}

	result = derive_reference_offsets(
		grid=build_grid(cells),
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=build_scan_config(xy_offset=(-1.0, 1.0)),
		candidates=[],
		stage_extension_mm=46.0,
	)

	assert result.samples[0].derived_intercept_mm == pytest.approx(10.0 + 1.875 - 9.6)
	assert result.samples[1].derived_intercept_mm == pytest.approx(12.0 - 0.085 - 9.8)
	assert stamped_datum_intercept_mm(result) == pytest.approx((2.275 + 2.115) / 2.0)


def test_datum_intercept_ignores_unstamped_cells_and_refuses_a_spread() -> None:
	cells = [
		build_cell(cell_idx=0, x=354.0, y=41.0, z_offset=10.0),
		build_cell(cell_idx=1, x=459.0, y=41.0, z_offset=14.0),
	]
	cells[0].calibration_point.z_captured_at_stage_mm = 46.0
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=463.0, tool_y=44.0),
		]
	)
	analysis.wall_depths_by_cell = {(0, 0): 9.6, (0, 1): 9.6}
	derive_args = dict(
		grid=build_grid(cells),
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=build_scan_config(xy_offset=(-1.0, 1.0)),
		candidates=[],
	)

	# Without a live stage nothing was carried, so no intercept is offered for installing.
	assert stamped_datum_intercept_mm(derive_reference_offsets(**derive_args)) is None

	result = derive_reference_offsets(**derive_args, stage_extension_mm=46.0)
	assert stamped_datum_intercept_mm(result) == pytest.approx(0.4)

	cells[1].calibration_point.z_captured_at_stage_mm = 46.0
	with pytest.raises(ValueError, match="z intercepts"):
		stamped_datum_intercept_mm(derive_reference_offsets(**derive_args, stage_extension_mm=46.0))


def test_datum_stamped_with_the_stage_retracted_is_refused() -> None:
	# Cell calibration retracts the stage before a tool move, so a z saved then is stamped ~0.5mm;
	# carrying it to 46mm would install an intercept of -45.1 on every cell.
	cells = [build_cell(cell_idx=0, x=354.0, y=41.0, z_offset=10.0)]
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)])
	analysis.wall_depths_by_cell = {(0, 0): 9.6}
	derive_args = dict(
		grid=build_grid(cells),
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=build_scan_config(xy_offset=(-1.0, 1.0)),
		candidates=[],
		stage_extension_mm=46.0,
	)

	cells[0].calibration_point.z_captured_at_stage_mm = 0.5
	result = derive_reference_offsets(**derive_args)
	with pytest.raises(ValueError, match=r"0:0 had its z saved at stage extension 0\.50mm.*46\.00mm.*shelf extension"):
		stamped_datum_intercept_mm(result)
	with pytest.raises(ValueError, match="cannot be carried"):
		check_datum_stage_carry(result)

	cells[0].calibration_point.z_captured_at_stage_mm = 49.0
	result = derive_reference_offsets(**derive_args)
	check_datum_stage_carry(result)
	assert stamped_datum_intercept_mm(result) == pytest.approx(10.0 + 3.0 - 9.6)


def test_start_check_counts_only_stamps_the_apply_can_carry() -> None:
	cells = [build_cell(cell_idx=0, x=354.0, y=41.0), build_cell(cell_idx=1, x=380.0, y=41.0, is_manual=False)]
	grid = build_grid(cells)
	check = dict(grid=grid, side=StorageModuleSide.FRONT, stage_extension_mm=46.0)

	assert check_stamped_reference_carry(**check) is False

	# A stamp on a cell that is not hand-calibrated is not a datum.
	cells[1].calibration_point.z_captured_at_stage_mm = 46.0
	assert check_stamped_reference_carry(**check) is False

	cells[0].calibration_point.z_captured_at_stage_mm = 49.0
	assert check_stamped_reference_carry(**check) is True

	cells[0].calibration_point.z_captured_at_stage_mm = 0.5
	with pytest.raises(ValueError, match=r"0:0 had its z saved at stage extension 0\.50mm.*46\.00mm"):
		check_stamped_reference_carry(**check)
	assert check_stamped_reference_carry(grid=grid, side=StorageModuleSide.FRONT, stage_extension_mm=None) is True
	# A narrow rescan checks only the cells it derives from.
	assert check_stamped_reference_carry(**check, shelf_idx=0, cell_idx=1) is False
	with pytest.raises(ValueError, match="0:0"):
		check_stamped_reference_carry(**check, shelf_idx=0, cell_idx=0)


def test_datum_intercept_beyond_the_plausible_z_delta_is_refused() -> None:
	cells = [build_cell(cell_idx=0, x=354.0, y=41.0, z_offset=40.0)]
	cells[0].calibration_point.z_captured_at_stage_mm = 46.0
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)])
	analysis.wall_depths_by_cell = {(0, 0): 9.6}

	result = derive_reference_offsets(
		grid=build_grid(cells),
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=build_scan_config(xy_offset=(-1.0, 1.0)),
		candidates=[],
		stage_extension_mm=46.0,
	)

	with pytest.raises(ValueError, match="implausible z intercept 30.4mm"):
		stamped_datum_intercept_mm(result)
