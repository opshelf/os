
import pytest

np = pytest.importorskip("numpy")

from openshelf_os.laser_calibration.scan import (  # noqa: E402
	ScanBounds,
	analyze_scan_capture,
	build_shelves_from_scan,
	create_scan_capture,
)
from openshelf_os.storage import (  # noqa: E402
	StorageModuleSide,
)


def build_synthetic_depth_rows() -> list[list[float | None]]:
	depth_rows: list[list[float | None]] = []

	for row_idx in range(20):
		row: list[float | None] = [48.0] * 27

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

	return depth_rows


def build_synthetic_capture():
	return create_scan_capture(
		side=StorageModuleSide.FRONT,
		bounds=ScanBounds(x_min=0.0, x_max=130.0, y_min=0.0, y_max=95.0),
		step_mm=5.0,
		settle_ms=25,
		samples_per_point=3,
		laser_offset_x_mm=2.0,
		laser_offset_y_mm=4.0,
		depth_rows=build_synthetic_depth_rows(),
	)


def assert_synthetic_analysis(analysis, shelves) -> None:
	assert analysis.wall_depth_mm == 48.0
	assert len(analysis.shelves) == 1
	assert len(analysis.shelves[0].cells) == 3
	assert analysis.shelves[0].cells[0].cell_idx == 0
	assert analysis.shelves[0].cells[1].cell_idx == 1
	assert analysis.shelves[0].cells[2].cell_idx == 2
	assert analysis.shelves[0].cells[0].center_x_mm == 20.0
	assert analysis.shelves[0].cells[1].center_x_mm == 65.0
	assert analysis.shelves[0].cells[2].center_x_mm == 110.0
	assert shelves[0].cells[0].calibration_point is not None
	assert shelves[0].cells[0].calibration_point.x == 22.0
	assert shelves[0].cells[2].calibration_point is not None
	assert shelves[0].cells[2].calibration_point.x == analysis.shelves[0].cells[2].tool_x_mm


def test_analyze_scan_capture_detects_enclosed_holes_and_rejects_boundary_gaps(tmp_path):
	capture = build_synthetic_capture()

	analysis = analyze_scan_capture(capture, base_dir=tmp_path)
	shelves = build_shelves_from_scan(
		side=StorageModuleSide.FRONT,
		analysis=analysis,
	)

	assert_synthetic_analysis(analysis, shelves)




def test_axis_positions_never_step_past_the_bound() -> None:
	from openshelf_os.laser_calibration.scan import build_axis_positions

	assert build_axis_positions(220.0, 950.0, 4.0)[-3:] == [944.0, 948.0, 950.0]
	assert build_axis_positions(950.0, 220.0, 4.0)[-2:] == [222.0, 220.0]
	assert build_axis_positions(0.0, 8.0, 2.0) == [0.0, 2.0, 4.0, 6.0, 8.0]
