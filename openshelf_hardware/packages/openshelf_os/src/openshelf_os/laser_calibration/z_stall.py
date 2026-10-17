"""Which scanned openings a calibration sweep must treat specially: the hand-calibrated datum
(the yardstick the measured z is checked against) and DISABLED cells (beyond the carriage travel).
Stored cells are matched to the scan's openings by key and by position.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from openshelf_os.storage import CellStatus, StorageModuleSide
from .core import get_gripper_xy_correction

PROBE_SHIELD_MATCH_WINDOW_MM = 25.0


def _probe_shield_cells(*, grid: Any, side: StorageModuleSide) -> tuple[set, set]:
	"""Stored cells a probe must leave alone, as (datum keys, DISABLED keys)."""
	datum_cells: set[tuple[int, int]] = set()
	disabled_cells: set[tuple[int, int]] = set()

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			key = (int(shelf.shelf_idx), int(cell.cell_idx))

			if cell.status == CellStatus.DISABLED:
				disabled_cells.add(key)
			elif getattr(cell, "is_manual", False) is True:
				datum_cells.add(key)

	return datum_cells, disabled_cells


def _shielded_positions(
	*,
	grid: Any,
	side: StorageModuleSide,
	datum_cells: set[tuple[int, int]],
	disabled_cells: set[tuple[int, int]],
	scan_config: Any,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
	"""Shielded stored points predicted into the detection frame: stored point minus the offset.

	`derive_reference_offsets` pairs a manual cell with the opening it predicts at `manual - offset`;
	the same prediction is what makes a position match survive the scan re-indexing the cells.
	"""
	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)
	offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])
	offset_y_mm = 0.0 if xy_offset is None else float(xy_offset[1])
	datum_positions: list[tuple[float, float]] = []
	disabled_positions: list[tuple[float, float]] = []

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			key = (int(shelf.shelf_idx), int(cell.cell_idx))

			if key not in datum_cells and key not in disabled_cells:
				continue

			point = cell.calibration_point

			if point is None:
				continue

			if key in disabled_cells:
				disabled_positions.append((float(point.x) - offset_x_mm, float(point.y) - offset_y_mm))
			else:
				datum_positions.append((float(point.x) - offset_x_mm, float(point.y) - offset_y_mm))

	return datum_positions, disabled_positions


def _scanned_cell_shield_kind(
	cell: Any,
	*,
	datum_cells: set[tuple[int, int]],
	disabled_cells: set[tuple[int, int]],
	datum_positions: list[tuple[float, float]],
	disabled_positions: list[tuple[float, float]],
) -> str | None:
	"""What shields a detected opening from the probe: "datum", "disabled", or nothing.

	Keys are tried first, but they do not survive a scan: a single-shelf scan re-indexes its shelf
	to 0, and `build_detected_shelves` numbers the openings it saw, so a stored index names a
	different opening as soon as one drops out. The position match covers both; it matters which
	one matched, because a datum is still probeable when it is all the scope holds.
	"""
	key = (int(cell.shelf_idx), int(cell.cell_idx))

	if key in disabled_cells:
		return "disabled"

	if key in datum_cells:
		return "datum"

	tool_x_mm = getattr(cell, "tool_x_mm", None)
	tool_y_mm = getattr(cell, "tool_y_mm", None)

	if tool_x_mm is None or tool_y_mm is None:
		return None

	def matches(positions: list[tuple[float, float]]) -> bool:
		return any(
			abs(float(tool_x_mm) - shielded_x_mm) <= PROBE_SHIELD_MATCH_WINDOW_MM
			and abs(float(tool_y_mm) - shielded_y_mm) <= PROBE_SHIELD_MATCH_WINDOW_MM
			for shielded_x_mm, shielded_y_mm in positions
		)

	if matches(disabled_positions) is True:
		return "disabled"

	if matches(datum_positions) is True:
		return "datum"

	return None


def excluded_probe_cells(
	*,
	grid: Any,
	side: StorageModuleSide,
	analysis: Any | None = None,
	scanned_cells: Iterable[tuple[int, int]] | None = None,
	scan_config: Any | None = None,
) -> set[tuple[int, int]]:
	"""Keys of the detected cells the grip-edge sweep must leave alone.

	Neither a reserved tray home nor a cell holding an item is excluded: a tray presents the same
	gripper interface and the same divider surface to the laser as an empty cell, and an item sits
	out of the gripper's way, so any opening measures the same grip edge. What is left out
	is a cell the rack does not actually have (DISABLED, i.e. beyond the carriage travel) and a
	hand-calibrated cell - that datum is the yardstick the fit is judged against, so it must stay
	out of the sample set.

	The datum is held out only while the scope still holds an independent opening to judge it, so a
	scope that holds nothing but the manual cell - the cheapest way to verify that cell's z - keeps
	the datum measurable instead of finding nothing to sweep.

	The returned keys are the ones `run_grip_edge_sweep` sees, i.e. the analysis' own:
	`analysis` is the in-scope scan, and a stored shielded cell is matched to a detected opening by
	key *and* by position (`_scanned_cell_is_shielded`). Without an analysis the whole side is the
	scope and `scanned_cells` may name cells in grid keys.
	"""
	datum_cells, disabled_cells = _probe_shield_cells(grid=grid, side=side)
	datum_positions: list[tuple[float, float]] = []
	disabled_positions: list[tuple[float, float]] = []

	if scan_config is not None:
		datum_positions, disabled_positions = _shielded_positions(
			grid=grid,
			side=side,
			datum_cells=datum_cells,
			disabled_cells=disabled_cells,
			scan_config=scan_config,
		)
	scanned: list[Any] = []

	if analysis is not None and len(getattr(analysis, "shelves", [])) > 0:
		scanned = [cell for shelf in analysis.shelves for cell in shelf.cells]
	elif scanned_cells is not None:
		scanned = [
			SimpleNamespace(shelf_idx=int(shelf_idx), cell_idx=int(cell_idx))
			for shelf_idx, cell_idx in scanned_cells
		]

	if len(scanned) == 0:
		# The whole side is the scope: every stored datum is held out, unless the side has nothing
		# independent left, in which case the datum is the only opening worth pressing.
		grid_has_independent_cell = any(
			(int(shelf.shelf_idx), int(cell.cell_idx)) not in datum_cells | disabled_cells
			for shelf in grid.get_side(side)
			for cell in shelf.cells
		)

		return disabled_cells | (datum_cells if grid_has_independent_cell is True else set())

	scanned_keys = [(int(cell.shelf_idx), int(cell.cell_idx)) for cell in scanned]
	kinds = [
		_scanned_cell_shield_kind(
			cell,
			datum_cells=datum_cells,
			disabled_cells=disabled_cells,
			datum_positions=datum_positions,
			disabled_positions=disabled_positions,
		)
		for cell in scanned
	]
	has_independent_cell = any(kind is None for kind in kinds)
	excluded: set[tuple[int, int]] = set()

	for index, kind in enumerate(kinds):
		if kind is None:
			continue

		if kind == "datum" and has_independent_cell is False:
			# Only the datum is in scope: this run verifies it by contact, so it is the point of
			# the run. Its agreement is then partly self-referential, which the snapshot shows.
			continue

		excluded.add(scanned_keys[index])

	return excluded


def datum_probe_cells(
	*,
	grid: Any,
	side: StorageModuleSide,
	analysis: Any,
	scan_config: Any,
) -> set[tuple[int, int]]:
	"""Analysis keys of the scanned openings that are a hand-calibrated datum (not disabled)."""
	datum_cells, disabled_cells = _probe_shield_cells(grid=grid, side=side)
	datum_positions, disabled_positions = _shielded_positions(
		grid=grid,
		side=side,
		datum_cells=datum_cells,
		disabled_cells=disabled_cells,
		scan_config=scan_config,
	)

	return {
		(int(cell.shelf_idx), int(cell.cell_idx))
		for shelf in getattr(analysis, "shelves", [])
		for cell in shelf.cells
		if _scanned_cell_shield_kind(
			cell,
			datum_cells=datum_cells,
			disabled_cells=disabled_cells,
			datum_positions=datum_positions,
			disabled_positions=disabled_positions,
		)
		== "datum"
	}
