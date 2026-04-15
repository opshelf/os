from perpetual_os1.laser_calibration import ShelfSurfaceEdge, is_assumed_floor_shelf_edge
from perpetual_os1.laser_scan import ScanBounds


def test_is_assumed_floor_shelf_edge_matches_appended_floor_edge() -> None:
	bounds = ScanBounds(x_min=0.0, x_max=945.0, y_min=0.0, y_max=1200.0)
	edge = ShelfSurfaceEdge(x_mm=0.0, y_mm=0.0)

	assert is_assumed_floor_shelf_edge(bounds=bounds, edge=edge) is True


def test_is_assumed_floor_shelf_edge_ignores_real_shelf_edges() -> None:
	bounds = ScanBounds(x_min=0.0, x_max=945.0, y_min=0.0, y_max=1200.0)
	edge = ShelfSurfaceEdge(x_mm=0.0, y_mm=24.25)

	assert is_assumed_floor_shelf_edge(bounds=bounds, edge=edge) is False


def test_is_assumed_floor_shelf_edge_ignores_nonzero_lower_bounds() -> None:
	bounds = ScanBounds(x_min=0.0, x_max=945.0, y_min=50.0, y_max=1200.0)
	edge = ShelfSurfaceEdge(x_mm=0.0, y_mm=50.0)

	assert is_assumed_floor_shelf_edge(bounds=bounds, edge=edge) is False
