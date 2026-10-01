from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.reference_offsets import (
	REFERENCE_DATUM_CELL_IDX,
	REFERENCE_DATUM_SHELF_IDX,
	REFERENCE_MATCH_WINDOW_MM,
	ReferenceOffsetResult,
	ReferenceOffsetSample,
	derive_reference_offsets,
	select_manual_reference_cells,
	select_reference_datum_cell,
	z_model_datum_agreement_mm,
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
	grid.front_module = [Shelf(shelf_idx=0, shelf_width=200.0, cells=cells)]
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
		Shelf(shelf_idx=5, shelf_width=200.0, cells=[build_cell(cell_idx=0, x=354.0, y=641.0)])
	]
	assert select_reference_datum_cell(grid=elsewhere, side=StorageModuleSide.FRONT) is None


def test_wall_depths_come_back_from_a_str_metadata_path(tmp_path) -> None:
	"""The run metadata records its path as a str; the read-back has to accept it.

	lb4a-2's failed front-side run: the candidates on disk held the wall depths, a str path made
	`load_json_file` raise, and the datum's intercept came back empty so the probe was refused.
	"""
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	detected = build_detected_cell(cell_idx=0, tool_x=358.0, tool_y=44.0)
	analysis = build_analysis([detected])
	metadata = {
		"candidates": [
			{
				"x_min": detected.center_x_mm - 20.0,
				"x_max": detected.center_x_mm + 20.0,
				"y_min": detected.center_y_mm - 20.0,
				"y_max": detected.center_y_mm + 20.0,
				"row_count": 10,
				"wall_depth_mm": 9.62,
				"wall_sample_count": 5,
			}
		]
	}
	metadata_path = tmp_path / "metadata.json"
	metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
	analysis.metadata_path = str(metadata_path)

	result = derive_reference_offsets(
		grid=grid,
		analysis=analysis,
		side=StorageModuleSide.FRONT,
		scan_config=build_scan_config(xy_offset=(-1.0, 1.0)),
	)

	assert result.samples[0].wall_depth_mm == pytest.approx(9.62)
	assert result.intercept_mm == pytest.approx(6.5 - 9.62)


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


def test_stale_offset_one_pitch_off_matches_the_opening_its_prediction_implies() -> None:
	"""The configured offset is the contract for which opening a manual cell matched to.

	An offset a full pitch stale predicts the neighbouring opening exactly, so the match is
	unambiguous: the derivation reports what the prediction implied (the configured value,
	+7 here) rather than the true -98. Only the spread guard catches that when several
	reference cells are involved; a single datum cannot argue with its own contract.
	"""
	grid = build_grid([build_cell(cell_idx=0, x=354.0, y=41.0)])
	analysis = build_analysis(
		[
			build_detected_cell(cell_idx=0, tool_x=452.0, tool_y=44.0),
			build_detected_cell(cell_idx=1, tool_x=347.0, tool_y=44.0),
		]
	)

	result = derive(grid, analysis, build_scan_config(xy_offset=(7.0, -3.0)))

	assert result.samples[0].detected_x_mm == pytest.approx(347.0)
	assert result.offset_x_mm == pytest.approx(7.0)
	assert result.offset_y_mm == pytest.approx(-3.0)
	assert result.configured_offset_x_mm == pytest.approx(7.0)


def test_match_window_stays_under_half_the_smallest_opening_pitch() -> None:
	# The rack's tightest pitch on the front module is ~105mm; half of it bounds a safe match.
	assert REFERENCE_MATCH_WINDOW_MM < 105.0 / 2.0


def test_scope_filters_select_manual_cells_only() -> None:
	grid = build_grid(
		[
			build_cell(cell_idx=0, x=354.0, y=41.0),
			build_cell(cell_idx=1, x=519.0, y=41.0, is_manual=False),
			build_cell(cell_idx=2, x=684.0, y=41.0),
		]
	)

	selected = select_manual_reference_cells(grid=grid, side=StorageModuleSide.FRONT)

	assert [int(cell.cell_idx) for _, cell in selected] == [0, 2]
	assert select_manual_reference_cells(
		grid=grid, side=StorageModuleSide.FRONT, cell_idx=2
	)[0][1].cell_idx == 2
	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.FRONT, shelf_idx=4) == []
	assert select_manual_reference_cells(grid=grid, side=StorageModuleSide.BACK) == []


def build_manual_reference(
	*,
	intercept_mm: float | None = -3.12,
	manual_x_mm: float = 354.0,
) -> ReferenceOffsetResult:
	sample = ReferenceOffsetSample(
		shelf_idx=0,
		cell_idx=0,
		manual_x_mm=manual_x_mm,
		manual_y_mm=41.0,
		detected_x_mm=manual_x_mm + 4.0,
		detected_y_mm=44.0,
		delta_x_mm=-4.0,
		delta_y_mm=-3.0,
		wall_depth_mm=None if intercept_mm is None else 9.5,
		derived_intercept_mm=intercept_mm,
	)

	return ReferenceOffsetResult(
		side=StorageModuleSide.FRONT,
		offset_x_mm=sample.delta_x_mm,
		offset_y_mm=sample.delta_y_mm,
		spread_x_mm=0.0,
		spread_y_mm=0.0,
		samples=[sample],
		intercept_mm=intercept_mm,
	)


def test_datum_agreement_judges_where_the_models_put_the_datum_cells() -> None:
	reference = build_manual_reference()

	# Equal intercepts, tilted fit: comparing bare intercepts would give 0, but at the datum
	# cell the two models disagree by (S - S_cfg) * x.
	assert z_model_datum_agreement_mm(
		intercept_mm=-3.12,
		slope_per_mm=0.005,
		configured_slope_per_mm=0.0,
		manual_reference=reference,
	) == pytest.approx(0.005 * 354.0)

	# A flat fit that puts the datum cell where the tilted datum model puts it agrees.
	assert z_model_datum_agreement_mm(
		intercept_mm=-3.12 + 0.002 * 354.0,
		slope_per_mm=0.0,
		configured_slope_per_mm=0.002,
		manual_reference=reference,
	) == pytest.approx(0.0)


def test_datum_agreement_is_none_without_a_datum() -> None:
	assert (
		z_model_datum_agreement_mm(
			intercept_mm=-3.12,
			slope_per_mm=0.0,
			configured_slope_per_mm=0.0,
			manual_reference=None,
		)
		is None
	)
	assert (
		z_model_datum_agreement_mm(
			intercept_mm=-3.12,
			slope_per_mm=0.0,
			configured_slope_per_mm=0.0,
			manual_reference=build_manual_reference(intercept_mm=None),
		)
		is None
	)


def test_datum_agreement_falls_back_to_bare_intercepts_without_usable_samples() -> None:
	reference = build_manual_reference()
	reference.samples = [
		ReferenceOffsetSample(
			shelf_idx=0,
			cell_idx=0,
			manual_x_mm=354.0,
			manual_y_mm=41.0,
			detected_x_mm=358.0,
			detected_y_mm=44.0,
			delta_x_mm=-4.0,
			delta_y_mm=-3.0,
		)
	]

	assert z_model_datum_agreement_mm(
		intercept_mm=0.0,
		slope_per_mm=0.0,
		configured_slope_per_mm=0.0,
		manual_reference=reference,
	) == pytest.approx(3.12)
