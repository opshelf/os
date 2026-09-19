from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openshelf_os.laser_calibration.scan import ScanDetectedCell, ScanDetectedShelf
from openshelf_os.laser_calibration.z_stall import (
	STAGE_BACKDRIVE_TOLERANCE_MM,
	ZStallResult,
	excluded_probe_cells,
	run_z_stall_anchor,
	select_z_stall_probes,
)
from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellItem,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleSide,
)


STAGE_PARAMS = {
	"ROTATION_FRONT": 183.0,
	"ROTATION_BACK": 3.0,
	"STAGE_EXTENSION_SHELF_FRONT": 47.0,
	"STAGE_EXTENSION_SHELF_BACK": 46.0,
	"Z_EXTENSION_PLACE": 160.5,
	"Z_MOVE_RPM": 500,
	"Z_MOVE_ACCEL": 750,
}


def build_scan_config(**overrides):
	values = {
		"z_stall_probe_x_shift_mm": 20.0,
		"z_stall_tray_engagement_mm": 0.71,
		"z_stall_probe_start_mm": 5.0,
		"z_stall_probe_fast_approach_mm": 130.0,
		"z_stall_probe_target_mm": 180.0,
		"z_stall_probe_step_mm": 0.5,
		"z_stall_probe_rpm": 20,
		"z_stall_probe_no_progress_s": 0.25,
		"z_stall_probe_timeout_s": 60.0,
		"z_stall_probe_repeats": 2,
		"z_stall_probe_repeat_tolerance_mm": 0.3,
		"laser_sample_settle_ms": 50,
	}
	values.update(overrides)

	return SimpleNamespace(**values)


def build_detected_cell(*, shelf_idx: int, cell_idx: int, tool_x: float, tool_y: float) -> ScanDetectedCell:
	return ScanDetectedCell(
		shelf_idx=shelf_idx,
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


def build_cell(*, cell_idx: int, tool_x: float, tool_y: float, shelf_idx: int = 0) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=CalibrationCoordinate(x=tool_x, y=tool_y, s=0, z_offset=6.5),
		dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
	)


class FakeTool:
	"""Enough of the tool for the anchor: parameters, the stage motor, and nothing else."""

	def __init__(self, *, stage_position_mm: float = 47.0, params: dict | None = None) -> None:
		values = dict(STAGE_PARAMS)
		values.update(params or {})
		self._params = values
		self._ms = SimpleNamespace(get_position=AsyncMock(return_value=stage_position_mm))
		self._mg = SimpleNamespace()
		self._mr = SimpleNamespace()

	async def get_param(self, name: str):
		return self._params.get(name)


def build_result(*, intercept_mm: float, slope_per_mm: float = 0.0, x_span_mm: float = 0.0) -> ZStallResult:
	sample = SimpleNamespace(
		shelf_idx=0,
		cell_idx=0,
		probe_x_mm=350.0,
		probe_y_mm=40.0,
		wall_depth_mm=9.6,
		z_contact_mm=160.5 + intercept_mm + 9.6 - 0.71,
		z_contact_repeat_mm=None,
		delta_mm=intercept_mm,
	)

	return ZStallResult(
		side=StorageModuleSide.LEFT,
		intercept_mm=intercept_mm,
		slope_per_mm=slope_per_mm,
		fit_source="constant",
		stage_extension_mm=47.0,
		z_place_mm=160.5,
		rotation_center_deg=183.0,
		samples=[sample],
	)


# --------------------------------------------------------------------------- #
#  Probe selection
# --------------------------------------------------------------------------- #


def build_shelf_cells(shelf_idx: int, *, tool_y: float, xs: list[float]) -> list[ScanDetectedCell]:
	return [
		build_detected_cell(shelf_idx=shelf_idx, cell_idx=cell_idx, tool_x=tool_x, tool_y=tool_y)
		for cell_idx, tool_x in enumerate(xs)
	]


def build_module_analysis() -> tuple[SimpleNamespace, dict[tuple[int, int], float]]:
	"""Three cells on each of three shelves: bottom (y=40), middle (y=640), top (y=1090)."""
	shelf_rows = [
		(0, 40.0, [200.0, 350.0, 500.0, 650.0, 800.0]),
		(3, 640.0, [200.0, 350.0, 500.0, 650.0, 800.0]),
		(6, 1090.0, [200.0, 350.0, 500.0, 650.0, 800.0]),
	]
	analysis = SimpleNamespace(
		shelves=[
			ScanDetectedShelf(
				shelf_idx=shelf_idx,
				center_y_mm=tool_y,
				shelf_height_mm=30.0,
				shelf_width_mm=400.0,
				cells=build_shelf_cells(shelf_idx, tool_y=tool_y, xs=xs),
			)
			for shelf_idx, tool_y, xs in shelf_rows
		]
	)
	wall_depths = {
		(cell.shelf_idx, cell.cell_idx): 9.6
		for shelf in analysis.shelves
		for cell in shelf.cells
	}

	return analysis, wall_depths


def test_spread_shelf_indices_covers_the_module() -> None:
	from openshelf_os.laser_calibration.z_stall import _spread_shelf_indices

	assert _spread_shelf_indices(7, 3) == [0, 3, 6]
	assert _spread_shelf_indices(9, 5) == [0, 2, 4, 6, 8]
	assert _spread_shelf_indices(7, 7) == [0, 1, 2, 3, 4, 5, 6]
	assert _spread_shelf_indices(7, 9) == [0, 1, 2, 3, 4, 5, 6]
	assert _spread_shelf_indices(1, 3) == [0]
	assert _spread_shelf_indices(3, 1) == [1]


def test_select_z_stall_probes_samples_one_cell_on_each_spread_shelf() -> None:
	analysis, wall_depths = build_module_analysis()

	probes = select_z_stall_probes(analysis, wall_depths, shelves_to_probe=3)

	# Bottom, middle and top shelf, one opening each, chosen so the set spans X.
	assert [(cell.shelf_idx, cell.cell_idx) for cell, _ in probes][:3] == [(0, 0), (3, 2), (6, 4)]
	assert [cell.tool_x_mm for cell, _ in probes][:3] == [200.0, 500.0, 800.0]


def test_select_z_stall_probes_keeps_the_other_shelves_as_reserves() -> None:
	analysis, wall_depths = build_module_analysis()
	shelf_two = build_shelf_cells(2, tool_y=440.0, xs=[200.0, 500.0, 800.0])
	analysis.shelves.append(
		ScanDetectedShelf(
			shelf_idx=2,
			center_y_mm=440.0,
			shelf_height_mm=30.0,
			shelf_width_mm=300.0,
			cells=shelf_two,
		)
	)
	for cell in shelf_two:
		wall_depths[(2, cell.cell_idx)] = 9.6

	probes = select_z_stall_probes(analysis, wall_depths, shelves_to_probe=3)

	# Four shelves, three of them sampled first and the fourth held back for a dropped point.
	assert len(probes) == 4
	assert [cell.shelf_idx for cell, _ in probes] == [0, 3, 6, 2]


def test_select_z_stall_probes_can_sample_every_shelf() -> None:
	analysis, wall_depths = build_module_analysis()

	probes = select_z_stall_probes(analysis, wall_depths, shelves_to_probe=7)

	assert len(probes) == 3
	assert [cell.shelf_idx for cell, _ in probes] == [0, 3, 6]


def test_select_z_stall_probes_returns_empty_without_wall_depths() -> None:
	analysis = SimpleNamespace(
		shelves=[
			SimpleNamespace(
				shelf_idx=0,
				center_y_mm=40.0,
				cells=[build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=200.0, tool_y=40.0)],
			)
		]
	)

	assert select_z_stall_probes(analysis, {}) == []


def test_select_z_stall_probes_skips_cells_whose_probe_x_exceeds_the_axis_limit() -> None:
	"""Real lb4-3 jam: L:3:6 sits at tool_x=960; the probe adds a +20mm shift -> 980, past the
	950mm axis limit. The axis clamped, putting the gripper over the cell's tray hole instead of
	the divider; it drove down into the hole and jammed the tray."""

	analysis = SimpleNamespace(
		shelves=[
			SimpleNamespace(
				shelf_idx=3,
				center_y_mm=995.0,
				cells=[
					SimpleNamespace(shelf_idx=3, cell_idx=0, center_x_mm=279.0, tool_x_mm=330.0),
					SimpleNamespace(shelf_idx=3, cell_idx=3, center_x_mm=593.0, tool_x_mm=644.0),
					SimpleNamespace(shelf_idx=3, cell_idx=6, center_x_mm=909.0, tool_x_mm=960.0),
				],
			)
		]
	)
	wall_depths = {(3, 0): 9.9, (3, 3): 9.9, (3, 6): 9.9}

	# x axis limit 950, probe shift 20 -> a probe cell must satisfy tool_x <= 930
	probes = select_z_stall_probes(analysis, wall_depths, 930.0)
	probed = [cell.cell_idx for cell, _ in probes]

	# The out-of-travel opening is dropped, leaving the shelf's remaining opening to be probed.
	assert 6 not in probed
	assert probed == [0]


def test_select_z_stall_probes_skips_cells_below_the_axis_lower_limit() -> None:
	analysis = SimpleNamespace(
		shelves=[
			SimpleNamespace(
				shelf_idx=0,
				center_y_mm=40.0,
				cells=[
					SimpleNamespace(shelf_idx=0, cell_idx=0, center_x_mm=279.0, tool_x_mm=330.0),
					SimpleNamespace(shelf_idx=0, cell_idx=1, center_x_mm=909.0, tool_x_mm=960.0),
				],
			)
		]
	)
	wall_depths = {(0, 0): 9.9, (0, 1): 9.9}

	probes = select_z_stall_probes(analysis, wall_depths, None, 400.0)

	assert [cell.cell_idx for cell, _ in probes] == [1]


def test_select_z_stall_probes_skips_excluded_cells() -> None:
	analysis, wall_depths = build_module_analysis()

	probes = select_z_stall_probes(
		analysis, wall_depths, excluded_cells={(0, 0), (3, 2), (6, 4)}
	)

	# Every primary pick on those shelves is excluded, so the shelf's next opening is used.
	assert [(cell.shelf_idx, cell.cell_idx) for cell, _ in probes][:3] == [(0, 1), (3, 1), (6, 3)]
	assert all(
		(cell.shelf_idx, cell.cell_idx) not in {(0, 0), (3, 2), (6, 4)} for cell, _ in probes
	)


def test_excluded_probe_cells_drops_only_disabled_cells_and_datums() -> None:
	grid = StorageGrid(filename="storage.json")
	grid.left_module = [
		Shelf(
			shelf_idx=0,
			shelf_height=30.0,
			shelf_width=200.0,
			cells=[
				build_cell(cell_idx=0, tool_x=350.0, tool_y=40.0),
				build_cell(cell_idx=1, tool_x=455.0, tool_y=40.0),
				build_cell(cell_idx=2, tool_x=560.0, tool_y=40.0),
				build_cell(cell_idx=3, tool_x=665.0, tool_y=40.0),
			],
		)
	]
	# A maintenance cell IS a production tray home, and a tray presents the same gripper interface
	# and the same divider to the laser as an empty cell, so it stays probeable - and an item in a
	# cell is out of the probe's way, so that stays probeable too.
	grid.left_module[0].cells[0].status = CellStatus.MAINTENANCE
	grid.left_module[0].cells[1].status = CellStatus.DISABLED
	grid.left_module[0].cells[2].item = CellItem(
		dimensions=Dimensions(length=50.0, width=40.0, height=100.0)
	)
	grid.left_module[0].cells[3].is_manual = True

	excluded = excluded_probe_cells(grid=grid, side=StorageModuleSide.LEFT)

	# Only the cell the rack does not have and the hand-calibrated datum.
	assert excluded == {(0, 1), (0, 3)}


def test_excluded_probe_cells_keeps_a_datum_only_scope_probeable() -> None:
	grid = StorageGrid(filename="storage.json")
	grid.left_module = [
		Shelf(
			shelf_idx=0,
			shelf_height=30.0,
			shelf_width=200.0,
			cells=[
				build_cell(cell_idx=0, tool_x=350.0, tool_y=40.0),
				build_cell(cell_idx=1, tool_x=455.0, tool_y=40.0),
			],
		)
	]
	grid.left_module[0].cells[0].is_manual = True

	# The whole side is the scope: the independent opening holds the datum out of the sample set.
	assert excluded_probe_cells(grid=grid, side=StorageModuleSide.LEFT) == {(0, 0)}

	# A scope that holds nothing but the datum keeps it probeable: verifying that datum by
	# contact is exactly what a run scoped to the manual cell asks for.
	assert (
		excluded_probe_cells(grid=grid, side=StorageModuleSide.LEFT, scanned_cells={(0, 0)}) == set()
	)

	# Any independent opening in scope still holds the datum out.
	assert (
		excluded_probe_cells(
			grid=grid, side=StorageModuleSide.LEFT, scanned_cells={(0, 0), (0, 1)}
		) == {(0, 0)}
	)


# --------------------------------------------------------------------------- #
#  Probe execution
# --------------------------------------------------------------------------- #


def group_cells_by_shelf(
	cells: list[ScanDetectedCell],
) -> list[tuple[int, list[ScanDetectedCell]]]:
	grouped: list[tuple[int, list[ScanDetectedCell]]] = []
	for cell in cells:
		if len(grouped) == 0 or grouped[-1][0] != cell.shelf_idx:
			grouped.append((cell.shelf_idx, []))

		grouped[-1][1].append(cell)

	return grouped


def build_probe_analysis(cells: list[ScanDetectedCell], wall_depths: list[float]) -> tuple:
	analysis = SimpleNamespace(
		shelves=[
			ScanDetectedShelf(
				shelf_idx=shelf_idx,
				center_y_mm=shelf_cells[0].tool_y_mm,
				shelf_height_mm=30.0,
				shelf_width_mm=500.0,
				cells=list(shelf_cells),
			)
			for shelf_idx, shelf_cells in group_cells_by_shelf(cells)
		],
		metadata_path=Path("/tmp/fake-z-stall-metadata.json"),
	)
	candidates = [
		SimpleNamespace(
			x_min=cell.center_x_mm - 13.0,
			x_max=cell.center_x_mm + 13.0,
			y_min=cell.tool_y_mm - 15.0,
			y_max=cell.tool_y_mm + 15.0,
			row_count=1,
			wall_depth_mm=wall_depth,
			wall_sample_count=4,
		)
		for cell, wall_depth in zip(cells, wall_depths)
	]

	return analysis, candidates


async def run_anchor(
	*,
	probe_contacts: list[list[float]] | None = None,
	cells: list[ScanDetectedCell] | None = None,
	wall_depths: list[float] | None = None,
	tool: FakeTool | None = None,
	scan_config: SimpleNamespace | None = None,
	excluded_cells: set[tuple[int, int]] | None = None,
	contact_error: bool = False,
):
	"""Run the anchor against one contact list per probe point, in probe order."""
	if cells is None:
		cells = [
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
			build_detected_cell(shelf_idx=3, cell_idx=0, tool_x=455.0, tool_y=640.0),
			build_detected_cell(shelf_idx=6, cell_idx=0, tool_x=560.0, tool_y=1090.0),
		]

	if probe_contacts is None:
		probe_contacts = [[170.0, 170.0] for _ in cells]

	if wall_depths is None:
		wall_depths = [9.6] * len(cells)

	analysis, candidates = build_probe_analysis(cells, wall_depths)
	probe_results = []
	for contacts in probe_contacts:
		for contact in contacts:
			if contact_error is True:
				probe_results.append((False, 0.0, "limit", None))
			elif isinstance(contact, tuple):
				probe_results.append(contact)
			else:
				probe_results.append((True, contact, "stall", None))

	with patch(
		"openshelf_os.laser_calibration.z_stall.load_json_file",
		return_value={},
	):
		with patch(
			"openshelf_os.laser_calibration.z_stall.load_candidates_from_metadata",
			return_value=candidates,
		):
			with patch(
				"openshelf_os.laser_calibration.angle.capture.enter_capture_mode",
				AsyncMock(),
			):
				with patch(
					"openshelf_os.laser_calibration.angle.robot_ops.probe_z_until_stall",
					AsyncMock(side_effect=probe_results),
				):
					return await run_z_stall_anchor(
						tool=tool or FakeTool(),
						analysis=analysis,
						scan_config=scan_config or build_scan_config(),
						side=StorageModuleSide.LEFT,
						candidates=candidates,
						excluded_cells=excluded_cells,
						x_axis_limit_mm=950.0,
						x_axis_lower_limit_mm=0.0,
						z_axis_limit_mm=180.0,
						xy_offset=(-1.25, -1.75),
					)


@pytest.mark.asyncio
async def test_anchor_fits_intercept_and_slope_from_three_probes() -> None:
	# Same plate depth everywhere, so every sample implies the same delta at three X positions.
	result = await run_anchor()

	assert isinstance(result, ZStallResult)
	assert result.sample_count == 3
	assert result.intercept_mm == pytest.approx(170.0 - 160.5 + 0.71 - 9.6)
	assert result.slope_per_mm == pytest.approx(0.0)
	assert result.fit_source == "constant"  # no tilt is fitted over this short a span
	assert result.stage_extension_mm == pytest.approx(47.0)
	assert result.z_place_mm == pytest.approx(160.5)
	assert result.x_span_mm == pytest.approx(210.0)  # 350..560 in the probe frame
	probe_xs = sorted(sample.probe_x_mm for sample in result.samples)
	assert probe_xs == pytest.approx([368.75, 473.75, 578.75])
	assert [(sample.shelf_idx, sample.cell_idx) for sample in result.samples] == [
		(0, 0),
		(3, 0),
		(6, 0),
	]


@pytest.mark.asyncio
async def test_anchor_probes_the_divider_beside_the_cell_not_the_hole() -> None:
	"""Every probe is shifted by z_stall_probe_x_shift_mm plus the laser-to-gripper offset."""
	result = await run_anchor()
	sample = result.samples[0]

	assert sample.probe_x_mm == pytest.approx(350.0 + (-1.25) + 20.0)
	assert sample.probe_y_mm == pytest.approx(40.0 + (-1.75))


@pytest.mark.asyncio
async def test_anchor_drops_a_probe_whose_repeat_contact_moved() -> None:
	# Two probes per point: the first cell stalls at the same place twice, the second does not.
	result = await run_anchor(
		probe_contacts=[[170.0, 170.0], [170.0, 170.4]],
		cells=[
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
			build_detected_cell(shelf_idx=3, cell_idx=1, tool_x=455.0, tool_y=640.0),
		],
	)

	assert result.sample_count == 1
	assert len(result.dropped_repeats) == 1
	assert "3:1" in result.dropped_repeats[0]
	assert "0.400mm apart" in result.dropped_repeats[0]


@pytest.mark.asyncio
async def test_anchor_drops_a_probe_that_stalls_once_then_misses() -> None:
	"""A stall followed by a miss leaves one contact, which is not evidence of a wall.

	The point is dropped (not sampled), and as the only probe it refuses the run: the raise is
	only reachable when sample_count is 0, and its text carries the dropped repeat.
	"""
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]

	with pytest.raises(ValueError, match="disagreed with itself") as excinfo:
		await run_anchor(
			probe_contacts=[[170.0, (False, 0.0, "limit", None)]],
			cells=cells,
		)

	assert "0:0 stalled at 170.000mm then did not stall again (limit)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_anchor_keeps_the_clean_probes_after_a_stall_then_miss() -> None:
	"""One bad repeat does not lose the run: a neighbour that stalls twice is still sampled."""
	result = await run_anchor(
		probe_contacts=[[170.0, (False, 0.0, "limit", None)], [170.0, 170.0]],
		cells=[
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
			build_detected_cell(shelf_idx=3, cell_idx=0, tool_x=455.0, tool_y=640.0),
		],
	)

	assert result.sample_count == 1
	assert [(sample.shelf_idx, sample.cell_idx) for sample in result.samples] == [(3, 0)]
	assert len(result.dropped_repeats) == 1
	assert "0:0 stalled at 170.000mm then did not stall again (limit)" in result.dropped_repeats[0]


@pytest.mark.asyncio
async def test_anchor_refuses_when_every_probe_moves_under_load() -> None:
	with pytest.raises(ValueError, match="moving under probe force"):
		await run_anchor(
			probe_contacts=[[170.0, 171.0], [170.0, 172.0]],
			cells=[
				build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
				build_detected_cell(shelf_idx=3, cell_idx=1, tool_x=455.0, tool_y=640.0),
			],
	)


@pytest.mark.asyncio
async def test_anchor_refuses_when_the_stage_is_pushed_back() -> None:
	tool = FakeTool(stage_position_mm=47.0 - (STAGE_BACKDRIVE_TOLERANCE_MM + 0.6))

	with pytest.raises(ValueError, match="pushed back"):
		await run_anchor(
			tool=tool,
			cells=[build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)],
		)


@pytest.mark.asyncio
async def test_anchor_refuses_a_fault_on_the_rapid_approach() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]
	analysis, candidates = build_probe_analysis(cells, [9.6])

	with patch("openshelf_os.laser_calibration.z_stall.load_json_file", return_value={}):
		with patch(
			"openshelf_os.laser_calibration.z_stall.load_candidates_from_metadata",
			return_value=candidates,
		):
			with patch("openshelf_os.laser_calibration.angle.capture.enter_capture_mode", AsyncMock()):
				with patch(
					"openshelf_os.laser_calibration.angle.robot_ops.probe_z_until_stall",
					AsyncMock(return_value=(False, 12.0, "fast_approach_fault", None)),
				):
					with pytest.raises(ValueError, match="faulted during the rapid approach"):
						await run_z_stall_anchor(
							tool=FakeTool(),
							analysis=analysis,
							scan_config=build_scan_config(),
							side=StorageModuleSide.LEFT,
							candidates=candidates,
						)


@pytest.mark.asyncio
async def test_anchor_skips_a_contact_shallower_than_the_rapid_approach() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]

	with pytest.raises(ValueError, match="no probe cell contacted a wall"):
		await run_anchor(probe_contacts=[[100.0, 100.0]], cells=cells)


@pytest.mark.asyncio
async def test_anchor_reports_a_wall_beyond_reach_when_the_target_is_the_axis_limit() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]

	with pytest.raises(ValueError, match="beyond the tool's reach"):
		await run_anchor(
			probe_contacts=[[0.0]],
			cells=cells,
			scan_config=build_scan_config(z_stall_probe_target_mm=180.0),
			contact_error=True,
		)


@pytest.mark.asyncio
async def test_anchor_refuses_an_implausible_intercept() -> None:
	with pytest.raises(ValueError, match="implausible z delta"):
		await run_anchor(probe_contacts=[[230.0, 230.0]], cells=[
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
		])


@pytest.mark.asyncio
async def test_anchor_refuses_when_every_cell_is_excluded() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]

	with pytest.raises(ValueError, match="disabled and hand-calibrated cells are excluded"):
		await run_anchor(probe_contacts=[[170.0, 170.0]], cells=cells, excluded_cells={(0, 0)})


@pytest.mark.asyncio
async def test_anchor_accepts_a_single_probe_as_a_tilt_free_constant() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]
	result = await run_anchor(cells=cells)

	assert result.sample_count == 1
	assert result.slope_per_mm == pytest.approx(0.0)
	assert result.fit_source == "constant"
	assert result.samples[0].z_contact_repeat_mm == pytest.approx(170.0)
	assert result.samples[0].repeat_delta_mm == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_anchor_skips_the_repeat_probe_when_repeats_are_disabled() -> None:
	cells = [build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0)]
	result = await run_anchor(
		probe_contacts=[[170.0]],
		cells=cells,
		scan_config=build_scan_config(z_stall_probe_repeats=1),
	)

	assert result.sample_count == 1
	assert result.samples[0].z_contact_repeat_mm is None


@pytest.mark.asyncio
async def test_anchor_stops_after_the_requested_shelves_and_leaves_the_reserves() -> None:
	"""A run asking for 3 of 5 shelves probes three, not five: reserves only replace a dropped shelf."""
	cells = [
		build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
		build_detected_cell(shelf_idx=1, cell_idx=0, tool_x=350.0, tool_y=290.0),
		build_detected_cell(shelf_idx=2, cell_idx=0, tool_x=350.0, tool_y=540.0),
		build_detected_cell(shelf_idx=3, cell_idx=0, tool_x=350.0, tool_y=790.0),
		build_detected_cell(shelf_idx=4, cell_idx=0, tool_x=350.0, tool_y=1090.0),
	]
	scan_config = build_scan_config(z_stall_probe_shelves=3)
	analysis, candidates = build_probe_analysis(cells, [9.6] * len(cells))
	probe_results = [
		(True, 170.0, "stall", None)
		for _ in range(len(cells) * scan_config.z_stall_probe_repeats)
	]

	with patch(
		"openshelf_os.laser_calibration.z_stall.load_json_file",
		return_value={},
	):
		with patch(
			"openshelf_os.laser_calibration.z_stall.load_candidates_from_metadata",
			return_value=candidates,
		):
			with patch(
				"openshelf_os.laser_calibration.angle.capture.enter_capture_mode",
				AsyncMock(),
			) as enter:
				with patch(
					"openshelf_os.laser_calibration.angle.robot_ops.probe_z_until_stall",
					AsyncMock(side_effect=probe_results),
				) as probe:
					result = await run_z_stall_anchor(
						tool=FakeTool(),
						analysis=analysis,
						scan_config=scan_config,
						side=StorageModuleSide.LEFT,
						candidates=candidates,
						x_axis_limit_mm=950.0,
						x_axis_lower_limit_mm=0.0,
						z_axis_limit_mm=180.0,
						xy_offset=(-1.25, -1.75),
					)

	# Five shelves hold one cell each; with three requested, the chosen shelves are the bottom,
	# middle and top, so exactly three probes run and the two reserves are never touched.
	assert probe.call_count == 3 * scan_config.z_stall_probe_repeats
	assert [(sample.shelf_idx, sample.cell_idx) for sample in result.samples] == [
		(0, 0),
		(2, 0),
		(4, 0),
	]
	# Two capture attempts per probe: only shelves 0, 2 and 4 were ever driven to.
	probed_tool_ys = sorted(call.kwargs["y_mm"] + 1.75 for call in enter.call_args_list)
	assert probed_tool_ys == pytest.approx([40.0, 40.0, 540.0, 540.0, 1090.0, 1090.0])


@pytest.mark.asyncio
async def test_anchor_fits_in_the_frame_the_model_is_applied_in() -> None:
	"""The probe aims at the opening plus the divider shift, but the stored model is evaluated at
	the cell's grab x, so the fit must use grab x - otherwise the model is tilted by the shift."""

	cells = [
		build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=341.25, tool_y=40.0),
		build_detected_cell(shelf_idx=3, cell_idx=0, tool_x=491.25, tool_y=640.0),
		build_detected_cell(shelf_idx=6, cell_idx=0, tool_x=641.25, tool_y=1090.0),
	]
	# A tilt of at least Z_DELTA_TILT_MIN_TOTAL_MM (0.75mm) over the span is what the fit trusts.
	delta_by_cell = {(0, 0): 1.0, (3, 0): 1.6, (6, 0): 2.2}
	analysis, candidates = build_probe_analysis(cells, [9.6] * len(cells))
	# contact = delta + wall + z_place - tray, with z_place 160.5 and tray 0.71.
	probe_results = [
		(True, delta_by_cell[(cell.shelf_idx, cell.cell_idx)] + 169.39, "stall", None)
		for cell in cells
		for _ in range(2)
	]

	with patch(
		"openshelf_os.laser_calibration.z_stall.load_json_file",
		return_value={},
	):
		with patch(
			"openshelf_os.laser_calibration.z_stall.load_candidates_from_metadata",
			return_value=candidates,
		):
			with patch(
				"openshelf_os.laser_calibration.angle.capture.enter_capture_mode",
				AsyncMock(),
			):
				with patch(
					"openshelf_os.laser_calibration.angle.robot_ops.probe_z_until_stall",
					AsyncMock(side_effect=probe_results),
				):
					result = await run_z_stall_anchor(
						tool=FakeTool(),
						analysis=analysis,
						scan_config=build_scan_config(),
						side=StorageModuleSide.LEFT,
						candidates=candidates,
						x_axis_limit_mm=950.0,
						x_axis_lower_limit_mm=0.0,
						z_axis_limit_mm=180.0,
						xy_offset=(-1.25, -1.75),
					)

	# Grab x 340..640 with deltas 1.0, 1.6, 2.2: slope 0.004, intercept 1.0 - 0.004 * 340 = -0.36.
	# Fitting on the aimed point (grab x + 20) would report -0.44 instead.
	assert result.slope_per_mm == pytest.approx(0.004)
	assert result.intercept_mm == pytest.approx(-0.36, abs=0.01)


def test_resolved_model_rebases_the_intercept_when_it_substitutes_a_slope() -> None:
	"""The canonical rule: a substituted slope moves the fit's intercept, not just its tilt."""
	from openshelf_os.laser_calibration.z_stall import ZStallResult, ZStallSample, resolve_z_stall_model

	samples = [
		ZStallSample(
			shelf_idx=shelf_idx,
			cell_idx=0,
			probe_x_mm=grab_x + 20.0,
			probe_y_mm=40.0,
			wall_depth_mm=9.6,
			z_contact_mm=170.0,
			z_contact_repeat_mm=170.0,
			delta_mm=delta_mm,
			reason="stall",
		)
		for shelf_idx, grab_x, delta_mm in ((0, 300.0, -2.2854), (3, 400.0, -2.0472))
	]
	result = ZStallResult(
		side=StorageModuleSide.LEFT,
		intercept_mm=-2.1663,
		slope_per_mm=0.0,
		fit_source="constant",
		stage_extension_mm=47.0,
		z_place_mm=160.5,
		rotation_center_deg=183.0,
		fit_centroid_x_mm=350.0,
		samples=samples,
	)
	scan_config = SimpleNamespace(
		left_wall_depth_to_gripper_z_model=SimpleNamespace(
			intercept_mm=-1.0,
			x_slope_mm_per_mm=0.002382,
			reference_stage_mm=47.0,
			reference_z_place_mm=160.5,
		),
		right_wall_depth_to_gripper_z_model=None,
	)

	decision = resolve_z_stall_model(result=result, scan_config=scan_config, side=StorageModuleSide.LEFT)

	assert decision.source == "config_short_span"
	assert decision.slope_per_mm == pytest.approx(0.002382)
	# -2.1663 was the delta at x = 350, so the installed intercept is that minus S * 350.
	assert decision.intercept_mm == pytest.approx(-2.1663 - 0.002382 * 350.0)
	# The installed line has to reproduce the two contacts the probe measured.
	assert decision.intercept_mm + decision.slope_per_mm * 300.0 == pytest.approx(-2.2854, abs=1e-3)
	assert decision.intercept_mm + decision.slope_per_mm * 400.0 == pytest.approx(-2.0472, abs=1e-3)


def test_resolved_model_keeps_a_tilt_the_probe_measured() -> None:
	from openshelf_os.laser_calibration.z_stall import ZStallResult, resolve_z_stall_model

	result = ZStallResult(
		side=StorageModuleSide.LEFT,
		intercept_mm=0.32,
		slope_per_mm=0.002,
		fit_source="tilt",
		stage_extension_mm=47.0,
		z_place_mm=160.5,
		rotation_center_deg=183.0,
		fit_centroid_x_mm=490.0,
		samples=[],
	)

	decision = resolve_z_stall_model(
		result=result,
		scan_config=SimpleNamespace(left_wall_depth_to_gripper_z_model=None, right_wall_depth_to_gripper_z_model=None),
		side=StorageModuleSide.LEFT,
	)

	assert decision.source == "probe"
	assert decision.slope_per_mm == pytest.approx(0.002)
	assert decision.intercept_mm == pytest.approx(0.32)


@pytest.mark.asyncio
async def test_anchor_drops_a_point_whose_middle_repeat_wanders() -> None:
	"""Every repeat has to agree, not just the first and the last.

	With three repeats a point can start and end on the same depth while a middle reading wanders
	by far more than the tolerance; the median would then be sampled as if the wall had held still.
	"""
	result = await run_anchor(
		probe_contacts=[[170.0, 181.0, 170.0], [170.0, 170.0, 170.0]],
		cells=[
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
			build_detected_cell(shelf_idx=3, cell_idx=1, tool_x=455.0, tool_y=640.0),
		],
		scan_config=build_scan_config(z_stall_probe_repeats=3),
	)

	assert result.sample_count == 1
	assert [(sample.shelf_idx, sample.cell_idx) for sample in result.samples] == [(3, 1)]
	assert len(result.dropped_repeats) == 1
	assert "181.000" in result.dropped_repeats[0]
	assert "11.000mm apart" in result.dropped_repeats[0]


def test_grid_built_analysis_feeds_the_reference_derivation() -> None:
	"""A rack that moved still needs its datum: wall depths must reach the derivation too."""
	from openshelf_os.laser_calibration.reference_offsets import derive_reference_offsets

	grid = StorageGrid(filename="storage.json")
	grid.left_module = [
		Shelf(
			shelf_idx=0,
			shelf_height=30.0,
			shelf_width=200.0,
			cells=[
				Cell(
					cell_idx=0,
					status=CellStatus.AVAILABLE,
					item=None,
					calibration_point=CalibrationCoordinate(
						x=357.25, y=38.5, s=0, z_offset=6.5, z_captured_at_stage_mm=47.0
					),
					dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
					is_manual=True,
				)
			],
		)
	]
	scan_config = SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		expected_hole_width_mm=26.0,
		expected_hole_height_mm=30.0,
		left_gripper_xy_correction_mm=SimpleNamespace(x_mm=-1.25, y_mm=-2.75),
		left_wall_depth_to_gripper_z_model=None,
	)
	analysis = SimpleNamespace(
		shelves=[SimpleNamespace(shelf_idx=0, cells=[], center_y_mm=0.0)],
		metadata_path=None,
		wall_depths_by_cell={},
	)
	built = run_from_grid(grid=grid, side=StorageModuleSide.LEFT, scan_config=scan_config)
	analysis = built

	result = derive_reference_offsets(
		grid=grid,
		analysis=analysis,
		side=StorageModuleSide.LEFT,
		scan_config=scan_config,
		candidates=[],
	)

	assert result.sample_count == 1
	assert result.intercept_mm == pytest.approx(6.5 - 9.62, abs=0.01)
	assert result.z_captured_stage_mm == pytest.approx(47.0)


def run_from_grid(*, grid: StorageGrid, side: StorageModuleSide, scan_config: SimpleNamespace):
	from openshelf_os.laser_calibration.z_stall import build_probe_analysis_from_grid

	return build_probe_analysis_from_grid(
		grid=grid,
		side=side,
		scan_config=scan_config,
		wall_depths={(0, 0): 9.62},
	)
