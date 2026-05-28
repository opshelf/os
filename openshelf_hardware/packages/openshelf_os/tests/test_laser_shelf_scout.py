from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.laser_calibration.shelf_scout import (
	ShelfSurfaceEdge,
	build_shelf_scout_x_positions,
	filter_shelf_edges,
	scan_shelf_scout_line,
)


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
		is_right_side=False,
		left_reference_x_mm=950.0,
	) == [950.0]
	assert build_shelf_scout_x_positions(
		x_min_mm=0.0,
		x_max_mm=900.0,
		is_right_side=False,
		scout_x_mm=123.0,
	) == [123.0]
