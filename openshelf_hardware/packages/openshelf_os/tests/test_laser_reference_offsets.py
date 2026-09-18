from __future__ import annotations

from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.reference_offsets import (
	REFERENCE_MATCH_WINDOW_MM,
	derive_reference_offsets,
	select_manual_reference_cells,
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
	grid.left_module = [Shelf(shelf_idx=0, shelf_height=30.0, shelf_width=200.0, cells=cells)]
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
				shelf_height_mm=30.0,
				shelf_width_mm=200.0,
				cells=list(detected_cells),
			)
		],
		metadata_path=None,
	)


def build_scan_config(xy_offset: tuple[float, float] | None = None) -> LaserScanConfig:
	config = LaserScanConfig()
	config.left_xy_offset = None if xy_offset is None else LaserScanXYOffsetConfig(
		x_mm=xy_offset[0], y_mm=xy_offset[1]
	)
	return config


def derive(grid: StorageGrid, analysis: SimpleNamespace, scan_config: LaserScanConfig):
	return derive_reference_offsets(
		grid=grid,
		analysis=analysis,
		side=StorageModuleSide.LEFT,
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


def test_refuses_when_no_cell_is_marked_manual() -> None:
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0, is_manual=False)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)])

	with pytest.raises(ValueError, match="marked manual"):
		derive(grid, analysis, build_scan_config(xy_offset=(-1.0, 1.0)))

	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.LEFT) == []


def test_refuses_when_the_reference_cell_matches_nothing() -> None:
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=520.0, tool_y=44.0)])

	with pytest.raises(ValueError, match="matched no manual reference cell"):
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


def test_hints_when_no_offset_is_configured_to_predict_from() -> None:
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis([build_detected_cell(cell_idx=0, tool_x=600.0, tool_y=300.0)])

	with pytest.raises(ValueError, match="set a rough one first"):
		derive(grid, analysis, build_scan_config(xy_offset=None))


def test_match_window_stays_under_half_the_smallest_opening_pitch() -> None:
	# The rack's tightest pitch on the left module is ~105mm; half of it bounds a safe match.
	assert REFERENCE_MATCH_WINDOW_MM < 105.0 / 2.0


def test_scope_filters_select_manual_cells_only() -> None:
	grid = build_grid(
		[
			build_cell(cell_idx=0, x=354.0, y=41.0),
			build_cell(cell_idx=1, x=519.0, y=41.0, is_manual=False),
			build_cell(cell_idx=2, x=684.0, y=41.0),
		]
	)

	selected = select_manual_reference_cells(grid=grid, side=StorageModuleSide.LEFT)

	assert [int(cell.cell_idx) for _, cell in selected] == [0, 2]
	assert select_manual_reference_cells(
		grid=grid, side=StorageModuleSide.LEFT, cell_idx=2
	)[0][1].cell_idx == 2
	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.LEFT, shelf_idx=4) == []
	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.RIGHT) == []
