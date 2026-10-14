from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.core import GapCandidateRegion
from openshelf_os.laser_calibration.fine_scan import capture_cross_scan
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.laser_calibration.shelf_scan import build_seeded_edge_probe_y_offsets_mm

WALL_DEPTH_MM = 9.8


class FakeAxis:
	def __init__(self) -> None:
		self.position_mm = 0.0

	async def move_to_position_no_wait(self, position_mm: float) -> None:
		self.position_mm = float(position_mm)


class FakeTool:
	"""Laser over a wall with one opening; reads 0 (open) inside it, the wall depth elsewhere."""

	def __init__(self, *, x_range: tuple[float, float], y_range: tuple[float, float]) -> None:
		self._mx = FakeAxis()
		self._my = FakeAxis()
		self._x_range = x_range
		self._y_range = y_range

	async def gantry_wait(self, **kwargs) -> None:
		return None

	async def get_laser_distance(self) -> float:
		x_mm = self._mx.position_mm
		y_mm = self._my.position_mm
		is_open = (
			self._x_range[0] <= x_mm <= self._x_range[1]
			and self._y_range[0] <= y_mm <= self._y_range[1]
		)
		return 0.0 if is_open else WALL_DEPTH_MM


async def _scan(tool: FakeTool, *, bounds: ScanBounds, candidate_y_mm: float):
	return await capture_cross_scan(
		tool,
		candidate=GapCandidateRegion(
			x_min=77.0,
			x_max=97.0,
			y_min=candidate_y_mm,
			y_max=candidate_y_mm,
			wall_depth_mm=WALL_DEPTH_MM,
			wall_sample_count=4,
		),
		bounds=bounds,
		step_mm=2.0,
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=WALL_DEPTH_MM,
		scan_config=SimpleNamespace(laser_offset_x_mm=51.0, laser_offset_y_mm=0.0),
		progress_prefix="fine_test",
	)


@pytest.mark.asyncio
async def test_capture_cross_scan_infers_bottom_edge_cut_off_by_axis_floor() -> None:
	# lb4a-2 back 0:0: the opening's top edge sits at y~29 and it stays open down to y=0.
	tool = FakeTool(x_range=(75.0, 101.0), y_range=(-5.0, 28.5))

	_, detected_cells = await _scan(
		tool,
		bounds=ScanBounds(x_min=67.0, x_max=107.0, y_min=0.0, y_max=52.875),
		candidate_y_mm=16.25,
	)

	assert len(detected_cells) == 1
	cell = detected_cells[0]
	assert cell.height_mm == pytest.approx(30.0)
	assert cell.center_y_mm == pytest.approx(14.0)
	assert cell.center_x_mm == pytest.approx(88.0, abs=1.0)


@pytest.mark.asyncio
async def test_capture_cross_scan_keeps_rejecting_open_bottom_above_axis_floor() -> None:
	# A window that does not start at the floor could have been extended, so no edge is guessed.
	tool = FakeTool(x_range=(75.0, 101.0), y_range=(-5.0, 128.5))

	_, detected_cells = await _scan(
		tool,
		bounds=ScanBounds(x_min=67.0, x_max=107.0, y_min=100.0, y_max=152.875),
		candidate_y_mm=116.25,
	)

	assert detected_cells == []


@pytest.mark.asyncio
async def test_capture_cross_scan_rejects_sliver_at_axis_floor() -> None:
	# Less than half a hole visible above the floor is not enough to place the cell.
	tool = FakeTool(x_range=(75.0, 101.0), y_range=(-20.0, 12.5))

	_, detected_cells = await _scan(
		tool,
		bounds=ScanBounds(x_min=67.0, x_max=107.0, y_min=0.0, y_max=52.875),
		candidate_y_mm=6.0,
	)

	assert detected_cells == []


@pytest.mark.asyncio
async def test_capture_cross_scan_measures_fully_visible_hole() -> None:
	tool = FakeTool(x_range=(75.0, 101.0), y_range=(250.0, 279.0))

	_, detected_cells = await _scan(
		tool,
		bounds=ScanBounds(x_min=67.0, x_max=107.0, y_min=237.0, y_max=290.0),
		candidate_y_mm=264.0,
	)

	assert len(detected_cells) == 1
	assert detected_cells[0].center_y_mm == pytest.approx(264.0, abs=1.0)


def test_seeded_edge_probe_offsets_add_lower_default_rows() -> None:
	assert build_seeded_edge_probe_y_offsets_mm([32.25, 44.25]) == [16.25, 32.25, 44.25]
	assert build_seeded_edge_probe_y_offsets_mm([16.25, 32.25]) == [16.25, 32.25]
