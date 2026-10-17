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
# lb4a-3 F0:0 (edge 5.5): 6.5 picked but less cleanly than 7.0.
GRIP_EDGE_TARGET_MARGIN_MM = 1.5
# Cells on one shelf differ (lb4a-3 F:3 by 1.25mm), so every cell is measured; after a shelf's first
# edge the search starts this far short of that edge instead of short of the laser's prediction.
GRIP_EDGE_SHELF_SEARCH_BELOW_MM = 2.0
# A shelf whose first cells all fail to grip is taken for multi-item bins and not probed further.
GRIP_EDGE_BIN_SHELF_MISSES = 2


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
	start_z_mm: float,
	max_z_mm: float,
	label: str,
	should_abort: Callable[[], bool] | None = None,
) -> float | None:
	"""The shallowest z the fingers grip at, or None when nothing grips steadily up to max_z_mm or
	the very first step already grips (the search never saw the edge).

	Coarse steps go deeper until two in a row grip (one is not enough: a multi-item bin or a
	misaligned opening produces single stray grips), then fine steps from the last miss find the edge.
	"""
	coordinate_type = type(coordinate)
	z_mm = float(start_z_mm)
	z_max_mm = float(max_z_mm)
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
	x_axis_limit_mm: float | None = None,
	x_axis_lower_limit_mm: float | None = None,
	should_abort: Callable[[], bool] | None = None,
) -> GripEdgeResult:
	"""Every cell's grip edge, shelf by shelf: the datum's cell first where the shelf holds it, then
	from the middle outwards."""
	from openshelf_os.common import AxisCoordinate  # noqa: PLC0415

	offset_x_mm, offset_y_mm = (0.0, 0.0) if xy_offset is None else (float(xy_offset[0]), float(xy_offset[1]))
	target_position = "front" if side == StorageModuleSide.FRONT else "back"
	s_value = 0 if side == StorageModuleSide.FRONT else 1
	result = GripEdgeResult()
	await tool._gs.safe_open()

	def reachable(cell: Any) -> bool:
		# The x axis clamps silently: a grab past either limit extends into the divider beside
		# the hole (lb4-3 jammed a tray that way). A fresh scan marks such cells DISABLED only later.
		grab_x_mm = float(cell.tool_x_mm) + offset_x_mm

		if (x_axis_limit_mm is not None and grab_x_mm > float(x_axis_limit_mm)) or (
			x_axis_lower_limit_mm is not None and grab_x_mm < float(x_axis_lower_limit_mm)
		):
			print_status(f"grip_edge=skip cell={cell.shelf_idx}:{cell.cell_idx} reason=unreachable x_mm={grab_x_mm:.1f}")
			return False

		return True

	for shelf in sorted(analysis.shelves, key=lambda current: float(current.center_y_mm)):
		cells = [
			cell
			for cell in sorted(shelf.cells, key=lambda current: float(current.center_x_mm))
			if (int(cell.shelf_idx), int(cell.cell_idx)) not in excluded_cells
			and float(wall_depths.get((int(cell.shelf_idx), int(cell.cell_idx)), 0.0)) > 0.0
			and reachable(cell)
		]
		middle = len(cells) // 2
		ordered = sorted(
			cells,
			key=lambda cell: (
				(int(cell.shelf_idx), int(cell.cell_idx)) not in preferred_cells,
				abs(cells.index(cell) - middle),
			),
		)
		# The shelf's own intercept once one cell has shown it: the laser's prediction misses it by
		# up to ~4mm, a neighbouring tray by ~1mm.
		shelf_intercept_mm: float | None = None
		misses = 0

		for cell in ordered:
			if shelf_intercept_mm is None and misses >= GRIP_EDGE_BIN_SHELF_MISSES:
				result.skipped.append(f"shelf {int(shelf.shelf_idx)} (no cell grips; multi-item bins)")
				print_status(f"grip_edge=skip shelf={int(shelf.shelf_idx)} reason=no_cell_grips")
				break

			key = (int(cell.shelf_idx), int(cell.cell_idx))
			wall_mm = float(wall_depths[key])
			grab_x_mm = float(cell.tool_x_mm) + offset_x_mm
			grab_y_mm = float(cell.tool_y_mm) + offset_y_mm
			coordinate = AxisCoordinate(x=grab_x_mm, y=grab_y_mm, s=s_value, z_offset=0.0)
			laser_z_mm = wall_mm + float(intercept_mm) + float(slope_per_mm) * grab_x_mm
			await tool.move(coordinate)
			await tool.rotate_to_position(target_position)
			edge_mm = None

			if shelf_intercept_mm is not None:
				shelf_z_mm = wall_mm + shelf_intercept_mm + float(slope_per_mm) * grab_x_mm
				edge_mm = await find_grip_edge(
					tool,
					coordinate=coordinate,
					start_z_mm=shelf_z_mm - GRIP_EDGE_TARGET_MARGIN_MM - GRIP_EDGE_SHELF_SEARCH_BELOW_MM,
					max_z_mm=laser_z_mm + GRIP_EDGE_SEARCH_ABOVE_MM,
					label=f"{key[0]}:{key[1]}",
					should_abort=should_abort,
				)

			if edge_mm is None:
				edge_mm = await find_grip_edge(
					tool,
					coordinate=coordinate,
					start_z_mm=laser_z_mm - GRIP_EDGE_SEARCH_BELOW_MM,
					max_z_mm=laser_z_mm + GRIP_EDGE_SEARCH_ABOVE_MM,
					label=f"{key[0]}:{key[1]}",
					should_abort=should_abort,
				)

			if edge_mm is None:
				misses += 1
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
			shelf_intercept_mm = sample.target_z_mm - wall_mm - float(slope_per_mm) * grab_x_mm
			print_status(
				f"grip_edge=sample cell={key[0]}:{key[1]} wall_mm={wall_mm:.3f} edge_z_mm={edge_mm:.2f} "
				f"target_z_mm={sample.target_z_mm:.2f}"
			)

	return result
