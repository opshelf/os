from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.laser_calibration.shelf_scout import (
	ShelfSurfaceEdge,
	filter_shelf_edges,
	scan_shelf_scout_line,
)
from openshelf_os.laser_calibration.shelf_scan import (
	resolve_lower_boundary_shelf_edge,
)
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.laser_calibration.core import StorageModuleSide


@pytest.mark.asyncio
async def test_scan_shelf_scout_line_detects_positive_to_zero_shelf_edge() -> None:
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
	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=93.0)]


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


def test_resolve_lower_boundary_shelf_edge_skips_nearby_detected_edge() -> None:
	bounds = ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1165.0)

	edge = resolve_lower_boundary_shelf_edge(
		bounds=bounds,
		side=StorageModuleSide.FRONT,
		shelf_edges=[ShelfSurfaceEdge(x_mm=950.0, y_mm=12.0)],
		hole_probe_y_offsets_mm=[32.25],
	)

	assert edge is None
