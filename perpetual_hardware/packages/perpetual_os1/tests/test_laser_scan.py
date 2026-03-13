import json

from perpetual_os1.laser_scan import (
	ScanBounds,
	analyze_scan_capture,
	build_shelves_from_scan,
	create_scan_capture,
)
from perpetual_os1.storage import StorageGrid, StorageModuleSide


def test_storage_grid_infers_sparse_indices_from_legacy_payload(tmp_path):
	grid_path = tmp_path / "storage.json"
	grid_path.write_text(
		json.dumps(
			{
				"left_module": [
					{
						"shelf_height": 200.0,
						"shelf_width": 240.0,
						"cells": [
							{
								"status": "available",
								"item": None,
								"calibration_point": {"x": 100.0, "y": 200.0, "s": 0, "z_offset": 0.0},
								"dimensions": {"length": 100.0, "width": 50.0, "height": 120.0},
							},
							{
								"status": "available",
								"item": None,
								"calibration_point": {"x": 50.0, "y": 200.0, "s": 0, "z_offset": 0.0},
								"dimensions": {"length": 100.0, "width": 50.0, "height": 120.0},
							},
						],
					}
				],
				"right_module": [],
			}
		)
	)

	grid = StorageGrid.load_from_file(str(grid_path))

	assert grid.left_module[0].shelf_idx == 0
	assert grid.left_module[0].cells[0].cell_idx == 0
	assert grid.left_module[0].cells[1].cell_idx == 1


def test_analyze_scan_capture_detects_enclosed_holes_and_rejects_boundary_gaps(tmp_path):
	depth_rows = []

	for row_idx in range(20):
		row = [48.0] * 27

		if 2 <= row_idx <= 7:
			for col_idx in range(2, 7):
				row[col_idx] = None
			for col_idx in range(11, 16):
				row[col_idx] = None
			for col_idx in range(20, 25):
				row[col_idx] = None

		if 0 <= row_idx <= 7:
			for col_idx in range(8, 10):
				row[col_idx] = None

		depth_rows.append(row)

	capture = create_scan_capture(
		side=StorageModuleSide.LEFT,
		bounds=ScanBounds(x_min=0.0, x_max=130.0, y_min=0.0, y_max=95.0),
		step_mm=5.0,
		settle_ms=25,
		samples_per_point=3,
		laser_offset_x_mm=2.0,
		laser_offset_y_mm=4.0,
		depth_rows=depth_rows,
	)

	analysis = analyze_scan_capture(capture, base_dir=tmp_path)
	shelves = build_shelves_from_scan(
		side=StorageModuleSide.LEFT,
		analysis=analysis,
	)

	assert analysis.wall_depth_mm == 48.0
	assert len(analysis.shelves) == 1
	assert len(analysis.shelves[0].cells) == 3
	assert analysis.shelves[0].cells[0].cell_idx == 0
	assert analysis.shelves[0].cells[1].cell_idx == 1
	assert analysis.shelves[0].cells[2].cell_idx == 2
	assert analysis.shelves[0].cells[0].center_x_mm == 110.0
	assert analysis.shelves[0].cells[1].center_x_mm == 65.0
	assert analysis.shelves[0].cells[2].center_x_mm == 20.0
	assert shelves[0].cells[0].calibration_point is not None
	assert shelves[0].cells[0].calibration_point.x == analysis.shelves[0].cells[0].tool_x_mm
	assert shelves[0].cells[2].calibration_point is not None
	assert shelves[0].cells[2].calibration_point.x == 18.0
