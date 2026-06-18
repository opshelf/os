from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import openshelf_os.laser_calibration.shelf_scan as shelf_scan_module

from openshelf_os.laser_calibration.shelf_scout import (
	ShelfSurfaceEdge,
	build_shelf_scout_x_positions,
	filter_shelf_edges,
	scan_shelf_scout_line,
)
from openshelf_os.laser_calibration.shelf_scan import (
	build_shelf_scout_x_positions as build_scan_shelf_scout_x_positions,
)
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.laser_calibration.core import StorageModuleSide


@pytest.mark.asyncio
async def test_scan_shelf_scout_line_detects_bounded_shelf_edge() -> None:
	depth_samples = iter([None, 5.0, 5.1, 5.2, 5.1, None, None])

	async def didSampleDepth(*args, **kwargs):
		return next(depth_samples)

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		gantry_wait=AsyncMock(),
	)

	depth_values, edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=[110.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0],
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		sample_depth=didSampleDepth,
	)

	assert depth_values == [None, 5.0, 5.1, 5.2, 5.1, None, None]
	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=105.0)]


@pytest.mark.asyncio
async def test_scan_shelf_scout_line_uses_configured_brim_max_height() -> None:
	depths = [None] + [5.0 for _ in range(11)] + [None]
	y_positions = [float(y_mm) for y_mm in range(120, 94, -2)]

	async def didSampleDepth(*args, **kwargs):
		return depths[y_positions.index(current_y_mm)]

	current_y_mm = y_positions[0]

	async def didMoveY(y_mm):
		nonlocal current_y_mm
		current_y_mm = float(y_mm)

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock(side_effect=didMoveY)),
		gantry_wait=AsyncMock(),
	)

	_, default_edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		sample_depth=didSampleDepth,
	)

	_, configured_edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		sample_depth=didSampleDepth,
		brim_max_height_mm=22.0,
	)

	assert default_edges == []
	assert configured_edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=108.0)]


@pytest.mark.asyncio
async def test_scan_shelf_scout_wrapper_passes_configured_brim_max_height(monkeypatch) -> None:
	depths = [None] + [5.0 for _ in range(11)] + [None]
	y_positions = [float(y_mm) for y_mm in range(120, 94, -2)]
	current_y_mm = y_positions[0]

	async def didMoveY(y_mm):
		nonlocal current_y_mm
		current_y_mm = float(y_mm)

	async def didSampleLaserDepth(*args, **kwargs):
		return depths[y_positions.index(current_y_mm)]

	monkeypatch.setattr(shelf_scan_module, 'sample_laser_depth', didSampleLaserDepth)

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock(side_effect=didMoveY)),
		gantry_wait=AsyncMock(),
	)

	_, edges = await shelf_scan_module.scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		brim_max_height_mm=22.0,
	)

	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=108.0)]

@pytest.mark.asyncio
async def test_scan_shelf_scout_line_uses_configured_post_edge_skip() -> None:
	current_y_mm = 0.0

	async def didMoveY(y_mm):
		nonlocal current_y_mm
		current_y_mm = float(y_mm)

	async def didSampleDepth(*args, **kwargs):
		if current_y_mm in {110.0, 108.0, 106.0, 104.0, 102.0}:
			return 5.0

		if current_y_mm in {60.0, 58.0, 56.0, 54.0, 52.0}:
			return 5.0

		return None

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock(side_effect=didMoveY)),
		gantry_wait=AsyncMock(),
	)

	y_positions = [float(y_mm) for y_mm in range(120, -2, -2)]

	depth_values, edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		sample_depth=didSampleDepth,
		post_edge_skip_mm=40.0,
	)

	assert edges == [
		ShelfSurfaceEdge(x_mm=25.0, y_mm=106.0),
		ShelfSurfaceEdge(x_mm=25.0, y_mm=56.0),
	]
	assert depth_values[y_positions.index(60.0)] == 5.0


@pytest.mark.asyncio
async def test_scan_shelf_scout_line_stops_after_first_edge_when_requested() -> None:
	depths = [None, 5.0, 5.1, 5.2, 5.1, None, None, 6.0, 6.1]
	sampled_count = 0

	async def didSampleDepth(*args, **kwargs):
		nonlocal sampled_count
		depth_mm = depths[sampled_count]
		sampled_count += 1
		return depth_mm

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		gantry_wait=AsyncMock(),
	)

	depth_values, edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=[110.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 96.0, 94.0],
		settle_ms=0,
		samples_per_point=1,
		progress_label='shelf_scout_1',
		sample_depth=didSampleDepth,
		stop_after_first_edge=True,
	)

	assert sampled_count == 6
	assert depth_values == [None, 5.0, 5.1, 5.2, 5.1, None, None, None, None]
	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=105.0)]


def test_filter_shelf_edges_discards_top_margin_and_close_duplicates() -> None:
	filtered_edges = filter_shelf_edges(
		edges=[
			ShelfSurfaceEdge(x_mm=25.0, y_mm=98.0),
			ShelfSurfaceEdge(x_mm=25.0, y_mm=80.0),
			ShelfSurfaceEdge(x_mm=25.0, y_mm=48.0),
			ShelfSurfaceEdge(x_mm=25.0, y_mm=35.0),
		],
		y_max_mm=100.0,
	)

	assert filtered_edges == [
		ShelfSurfaceEdge(x_mm=25.0, y_mm=80.0),
		ShelfSurfaceEdge(x_mm=25.0, y_mm=35.0),
	]


def test_build_shelf_scout_x_positions_preserves_right_zero_and_reference_behavior() -> None:
	assert build_shelf_scout_x_positions(
		x_min_mm=0.0,
		x_max_mm=900.0,
		is_right_side=True,
	) == [0.0]
	assert build_shelf_scout_x_positions(
		x_min_mm=0.0,
		x_max_mm=900.0,
		is_right_side=True,
		scout_x_mm=950.0,
	) == [950.0]
	assert build_shelf_scout_x_positions(
		x_min_mm=0.0,
		x_max_mm=900.0,
		is_right_side=False,
		left_reference_x_mm=950.0,
	) == [950.0]
	assert build_shelf_scout_x_positions(
		x_min_mm=0.0,
		x_max_mm=900.0,
		is_right_side=False,
		scout_x_mm=123.0,
	) == [123.0]


def test_scan_shelf_scout_x_positions_uses_configured_side_override() -> None:
	bounds = ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1295.0)

	assert build_scan_shelf_scout_x_positions(
		bounds=bounds,
		side=StorageModuleSide.RIGHT,
		scan_config=SimpleNamespace(right_shelf_scout_x_mm=950.0),
	) == [950.0]
