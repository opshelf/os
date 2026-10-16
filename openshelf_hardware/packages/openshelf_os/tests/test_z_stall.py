from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openshelf_os.laser_calibration.scan import ScanDetectedCell, ScanDetectedShelf
from openshelf_os.laser_calibration.z_stall import (
	STAGE_BACKDRIVE_TOLERANCE_MM,
	run_z_stall_anchor,
	select_z_stall_probes,
)
from openshelf_os.storage import (
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


# --------------------------------------------------------------------------- #
#  Probe selection
# --------------------------------------------------------------------------- #


def test_select_z_stall_probes_skips_cells_whose_probe_x_exceeds_the_axis_limit() -> None:
	"""Real lb4-3 jam: F:3:6 sits at tool_x=960; the probe adds a +20mm shift -> 980, past the
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
						side=StorageModuleSide.FRONT,
						candidates=candidates,
						excluded_cells=excluded_cells,
						x_axis_limit_mm=950.0,
						x_axis_lower_limit_mm=0.0,
						z_axis_limit_mm=180.0,
						xy_offset=(-1.25, -1.75),
					)


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
							side=StorageModuleSide.FRONT,
							candidates=candidates,
						)


@pytest.mark.asyncio
async def test_anchor_refuses_an_implausible_intercept() -> None:
	with pytest.raises(ValueError, match="implausible z delta"):
		await run_anchor(probe_contacts=[[230.0, 230.0]], cells=[
			build_detected_cell(shelf_idx=0, cell_idx=0, tool_x=350.0, tool_y=40.0),
		])
