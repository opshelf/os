from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from openshelf_os.laser_calibration.scan import (  # noqa: E402
	ScanBounds,
	analyze_scan_capture,
	build_shelves_from_scan,
	create_scan_capture,
)
from openshelf_os.storage import (  # noqa: E402
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleSide,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SYNTHETIC_LASER_SCAN_PATH = FIXTURES_DIR / "laser_scan_synthetic.npy"


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


def test_build_shelves_from_scan_preserves_existing_dimensions(tmp_path):
	capture = build_synthetic_capture()

	analysis = analyze_scan_capture(capture, base_dir=tmp_path)
	existing_shelf = Shelf(
		shelf_idx=0,
		shelf_width=789.0,
		cells=[
			Cell(
				cell_idx=cell_idx,
				status=CellStatus.AVAILABLE,
				item=None,
				calibration_point=CalibrationCoordinate(x=1.0, y=2.0, z_offset=3.0, s=0),
				dimensions=Dimensions(length=155.0, width=125.0, height=100.0),
			)
			for cell_idx in range(3)
		],
	)

	shelves = build_shelves_from_scan(
		side=StorageModuleSide.FRONT,
		analysis=analysis,
		existing_shelves=[existing_shelf],
	)

	assert shelves[0].shelf_width == pytest.approx(789.0)
	for cell in shelves[0].cells:
		assert cell.dimensions == Dimensions(length=155.0, width=125.0, height=100.0)
		assert cell.calibration_point is not None
		assert cell.calibration_point.x != pytest.approx(1.0)
		assert cell.calibration_point.z_offset == pytest.approx(3.0)

def test_analyze_scan_capture_accepts_synthetic_npy_fixture(tmp_path):
	depth_array = np.load(SYNTHETIC_LASER_SCAN_PATH)
	depth_rows: list[list[float | None]] = []

	for row in depth_array:
		depth_row: list[float | None] = []

		for value in row:
			float_value = float(value)

			if bool(np.isfinite(float_value)) is True:
				depth_row.append(float_value)
				continue

			depth_row.append(None)

		depth_rows.append(depth_row)

	capture = create_scan_capture(
		side=StorageModuleSide.FRONT,
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
		side=StorageModuleSide.FRONT,
		analysis=analysis,
	)

	assert_synthetic_analysis(analysis, shelves)
