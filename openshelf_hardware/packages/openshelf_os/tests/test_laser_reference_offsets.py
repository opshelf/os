from __future__ import annotations

from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.reference_offsets import (
	check_datum_stage_carry,
	derive_reference_offsets,
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


def test_datum_stamped_at_another_stage_extension_is_refused() -> None:
	# lb4a-3: the datum kept a 47.875mm stamp after the shelf extension moved to 46mm and its z was
	# re-validated there; carrying it put every derived cell 1.9mm too deep.
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

	cells[0].calibration_point.z_captured_at_stage_mm = 47.875
	result = derive_reference_offsets(**derive_args)
	with pytest.raises(ValueError, match=r"0:0 had its z saved at stage extension 47\.88mm.*46\.00mm.*shelf extension"):
		stamped_datum_intercept_mm(result)
	with pytest.raises(ValueError, match="cannot be carried"):
		check_datum_stage_carry(result)

	cells[0].calibration_point.z_captured_at_stage_mm = 46.5
	result = derive_reference_offsets(**derive_args)
	check_datum_stage_carry(result)
	assert stamped_datum_intercept_mm(result) == pytest.approx(10.0 + 0.5 - 9.6)


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
