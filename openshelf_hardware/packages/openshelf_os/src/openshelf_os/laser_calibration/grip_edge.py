"""Find each shelf's z from where the gripper actually starts to grip.

The laser measures every tray face, but where the gripper's hook-shaped fingers catch the hole
shifts per shelf by several millimetres (lb4a-3: -1.2 to -3.9mm against the wall depth), which no
single intercept fits. The gripper's own object detection shows it directly: too shallow and the
fingers splay without catching anything, and from the edge on they grip - deeper still reports a
grip while the hook drives the tray back, so the edge is the only sharp boundary there is. Each
probe point extends empty, splays the fingers, reads the result and closes them again before
retracting, so a tray is never withdrawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from openshelf_os.storage import StorageModuleSide

from .core import print_status, raise_if_cancelled

# The search starts this far shallower than the laser's prediction, where the fingers cannot reach
# the hole (lb4a-3's edges sat 1.2-3.9mm shallower than predicted), and stops just past it: the hook
# reports a grip however deep it goes, so a multi-item bin "grips" 5mm deep (lb4a-3 F2:3) and a
# deeper edge than the datum predicts is not one to trust.
GRIP_EDGE_SEARCH_BELOW_MM = 5.0
GRIP_EDGE_SEARCH_ABOVE_MM = 1.0
GRIP_EDGE_COARSE_STEP_MM = 1.0
GRIP_EDGE_FINE_STEP_MM = 0.25
# Just past the edge is the flat of the hook that seats the tray; further in, the hook only pushes.
GRIP_EDGE_TARGET_MARGIN_MM = 1.0


@dataclass
class GripEdgeSample:
	shelf_idx: int
	cell_idx: int
	grab_x_mm: float
	grab_y_mm: float
	wall_depth_mm: float
	edge_z_mm: float
	target_z_mm: float


@dataclass
class GripEdgeResult:
	samples: list[GripEdgeSample] = field(default_factory=list)
	skipped: list[str] = field(default_factory=list)


async def _grips_at(tool: Any, coordinate: Any, z_offset_mm: float, coordinate_type: Any) -> bool:
	probe = coordinate_type(x=coordinate.x, y=coordinate.y, s=coordinate.s, z_offset=float(z_offset_mm))
	await tool.extend_empty(probe)

	try:
		state = await tool._gs.open()
	finally:
		# Release before the retract: splayed fingers would drag the tray out with the tool.
		await tool._gs.close()

	await tool.retract_empty()

	return bool(state.gripped)


async def find_grip_edge(
	tool: Any,
	*,
	coordinate: Any,
	predicted_z_mm: float,
	label: str,
	should_abort: Callable[[], bool] | None = None,
) -> float | None:
	"""The shallowest z the fingers grip at, or None when nothing grips steadily.

	Coarse steps go deeper from well short of the prediction until two in a row grip (one is not
	enough: a multi-item bin or a misaligned opening produces single stray grips), then fine steps
	from the last miss find the edge.
	"""
	coordinate_type = type(coordinate)
	z_mm = float(predicted_z_mm) - GRIP_EDGE_SEARCH_BELOW_MM
	z_max_mm = float(predicted_z_mm) + GRIP_EDGE_SEARCH_ABOVE_MM
	last_miss_mm: float | None = None
	first_grip_mm: float | None = None

	while z_mm <= z_max_mm + 1e-6:
		raise_if_cancelled(should_abort)
		gripped = await _grips_at(tool, coordinate, z_mm, coordinate_type)
		print_status(f"grip_edge={label} z_mm={z_mm:.2f} gripped={gripped}")

		if gripped is False:
			last_miss_mm = z_mm
			first_grip_mm = None
		elif first_grip_mm is None:
			first_grip_mm = z_mm
		else:
			break

		z_mm += GRIP_EDGE_COARSE_STEP_MM
	else:
		return None

	if first_grip_mm is None or last_miss_mm is None:
		# Gripping from the very first step means the search never saw the edge.
		return None

	z_mm = last_miss_mm + GRIP_EDGE_FINE_STEP_MM

	while z_mm < first_grip_mm - 1e-6:
		raise_if_cancelled(should_abort)
		gripped = await _grips_at(tool, coordinate, z_mm, coordinate_type)
		print_status(f"grip_edge={label} z_mm={z_mm:.2f} gripped={gripped} fine=true")

		if gripped is True:
			return z_mm

		z_mm += GRIP_EDGE_FINE_STEP_MM

	return first_grip_mm


async def run_grip_edge_sweep(
	*,
	tool: Any,
	analysis: Any,
	wall_depths: dict[tuple[int, int], float],
	side: StorageModuleSide,
	xy_offset: tuple[float, float] | None,
	intercept_mm: float,
	slope_per_mm: float,
	preferred_cells: set[tuple[int, int]],
	excluded_cells: set[tuple[int, int]],
	should_abort: Callable[[], bool] | None = None,
) -> GripEdgeResult:
	"""One grip edge per shelf: the datum's cell where the shelf holds it, else its middle cell, then
	the others in turn until one grips steadily."""
	from openshelf_os.common import AxisCoordinate  # noqa: PLC0415

	offset_x_mm, offset_y_mm = (0.0, 0.0) if xy_offset is None else (float(xy_offset[0]), float(xy_offset[1]))
	target_position = "front" if side == StorageModuleSide.FRONT else "back"
	s_value = 0 if side == StorageModuleSide.FRONT else 1
	result = GripEdgeResult()
	await tool._gs.safe_open()

	for shelf in sorted(analysis.shelves, key=lambda current: float(current.center_y_mm)):
		cells = [
			cell
			for cell in sorted(shelf.cells, key=lambda current: float(current.center_x_mm))
			if (int(cell.shelf_idx), int(cell.cell_idx)) not in excluded_cells
			and float(wall_depths.get((int(cell.shelf_idx), int(cell.cell_idx)), 0.0)) > 0.0
		]
		middle = len(cells) // 2
		ordered = sorted(
			cells,
			key=lambda cell: (
				(int(cell.shelf_idx), int(cell.cell_idx)) not in preferred_cells,
				abs(cells.index(cell) - middle),
			),
		)

		for cell in ordered:
			key = (int(cell.shelf_idx), int(cell.cell_idx))
			wall_mm = float(wall_depths[key])
			grab_x_mm = float(cell.tool_x_mm) + offset_x_mm
			grab_y_mm = float(cell.tool_y_mm) + offset_y_mm
			coordinate = AxisCoordinate(x=grab_x_mm, y=grab_y_mm, s=s_value, z_offset=0.0)
			await tool.move(coordinate)
			await tool.rotate_to_position(target_position)
			edge_mm = await find_grip_edge(
				tool,
				coordinate=coordinate,
				predicted_z_mm=wall_mm + float(intercept_mm) + float(slope_per_mm) * grab_x_mm,
				label=f"{key[0]}:{key[1]}",
				should_abort=should_abort,
			)

			if edge_mm is None:
				result.skipped.append(f"{key[0]}:{key[1]} (no steady grip)")
				print_status(f"grip_edge=skip cell={key[0]}:{key[1]} reason=no_steady_grip")
				continue

			sample = GripEdgeSample(
				shelf_idx=key[0],
				cell_idx=key[1],
				grab_x_mm=grab_x_mm,
				grab_y_mm=grab_y_mm,
				wall_depth_mm=wall_mm,
				edge_z_mm=edge_mm,
				target_z_mm=edge_mm + GRIP_EDGE_TARGET_MARGIN_MM,
			)
			result.samples.append(sample)
			print_status(
				f"grip_edge=sample cell={key[0]}:{key[1]} wall_mm={wall_mm:.3f} edge_z_mm={edge_mm:.2f} "
				f"target_z_mm={sample.target_z_mm:.2f}"
			)
			break

	return result
