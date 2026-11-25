import asyncio
import json
from types import SimpleNamespace

import pytest

import openshelf_os.laser_calibration.io_box_scout as scout_module
from test_laser_flying_scan import FakeAxisMotor, FakeFlyingTool

PLATE_TOP_Y_MM = 420.0
PLATE_BOTTOM_Y_MM = 412.0
PLATE_RIGHT_EDGE_X_MM = 55.0
PLATE_FACE_DEPTH_MM = 95.0


def io_box_depth_mm(x_mm: float, y_mm: float) -> float:
	"""Front laser view of the box: plate face, nothing below it, the box interior above."""
	if y_mm < PLATE_BOTTOM_Y_MM:
		return 0.0

	if y_mm <= PLATE_TOP_Y_MM:
		return PLATE_FACE_DEPTH_MM if x_mm <= PLATE_RIGHT_EDGE_X_MM else 0.0

	return 300.0 if x_mm <= PLATE_RIGHT_EDGE_X_MM + 5.0 else 60.0


class FakeIOBoxTool(FakeFlyingTool):
	def __init__(self, params: dict) -> None:
		super().__init__()
		self._mx = FakeAxisMotor(position_mm=930.0, step_mm=0.5)
		self._my = FakeAxisMotor(position_mm=1.0, step_mm=0.5)

		for motor, lim in ((self._mx, 950.0), (self._my, 1165.0)):
			motor._params = SimpleNamespace(mpr=10.0, accel=0, lim=lim, lower_lim=0.0)

		self.params = params
		self.retract_count = 0

	async def get_laser_distance(self, *, timeout_s: float | None = None) -> float | None:
		await asyncio.sleep(0)
		return io_box_depth_mm(self._mx.position_mm, self._my.position_mm)

	async def get_param(self, name: str):
		return self.params.get(name)

	async def retract(self) -> None:
		self.retract_count += 1


class FakeIOBox:
	def __init__(self) -> None:
		self.events: list[str] = []

	async def open_door(self) -> None:
		self.events.append("open")

	async def home_door(self) -> None:
		self.events.append("home")


def build_scan_config() -> SimpleNamespace:
	return SimpleNamespace(
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		front_gripper_xy_correction_mm=SimpleNamespace(x_mm=-2.0, y_mm=1.0),
		front_wall_depth_to_gripper_z_model=SimpleNamespace(
			intercept_mm=-1.0,
			x_slope_mm_per_mm=0.0,
			reference_stage_mm=46.0,
			reference_z_place_mm=160.5,
		),
		fine_scan_feed_mm_s=40.0,
		flying_capture_latency_ms=0.0,
	)


@pytest.mark.asyncio
async def test_io_box_scout_measures_the_plate_and_leaves_the_door_homed(monkeypatch, tmp_path) -> None:
	"""Measures plate top, bottom, right edge and face depth in the tool frame, and closes the
	door again: a scout that left the door open would leave the box unguarded."""
	params = {
		"PICKUP_POSITION_L": [24, 467],
		"PICKUP_POSITION_H": [24, 700],
		"PICKUP_POSITION_MULTI_ITEM": [24, 465],
		"Z_EXTENSION_IO": 163.5,
		"Z_EXTENSION_IO_MULTI_ITEM": 174.0,
		"STAGE_EXTENSION_IO_BOX": 52.0,
		"STAGE_EXTENSION_IO_BOX_MULTI_ITEM": 52.0,
		"Z_EXTENSION_PLACE": 160.5,
	}
	tool = FakeIOBoxTool(params)
	io_box = FakeIOBox()

	async def no_scan_pose(*args, **kwargs) -> None:
		return None

	monkeypatch.setattr(scout_module, "enter_scan_mode", no_scan_pose)

	result = await scout_module.run_io_box_scout(
		robot=SimpleNamespace(_tool=tool, _io=io_box),
		scan_config=build_scan_config(),
		run_dir=tmp_path,
		x_travel_min_mm=0.0,
		x_travel_max_mm=950.0,
		y_travel_min_mm=0.0,
		y_travel_max_mm=1165.0,
	)

	assert result.plate_top_laser_y_mm == pytest.approx(PLATE_TOP_Y_MM, abs=1.0)
	assert result.plate_bottom_laser_y_mm == pytest.approx(PLATE_BOTTOM_Y_MM, abs=1.0)
	assert result.plate_thickness_matches is True
	assert result.plate_face_depth_mm == pytest.approx(PLATE_FACE_DEPTH_MM)
	assert result.plate_right_edge_laser_x_mm == pytest.approx(PLATE_RIGHT_EDGE_X_MM, abs=1.0)
	# Tool frame: x adds the laser offset and correction, y adds the correction.
	assert result.plate_right_edge_tool_x_mm == pytest.approx(PLATE_RIGHT_EDGE_X_MM + 51.0 - 2.0, abs=1.0)
	assert result.multi_item_y_minus_plate_top_mm == pytest.approx(465.0 - (PLATE_TOP_Y_MM + 1.0), abs=1.0)
	# Face depth through the model, carried from stage 46 to the IO box's 52.
	assert result.face_equivalent_z_multi_item_mm == pytest.approx(160.5 + 95.0 - 1.0 - 6.0)
	assert result.z_multi_item_minus_face_equivalent_mm == pytest.approx(174.0 - 248.5)
	assert io_box.events == ["open", "home"]
	assert tool.retract_count == 1
	saved = json.loads((tmp_path / scout_module.IO_BOX_SCOUT_RESULT_FILENAME).read_text())
	assert saved["plate_top_laser_y_mm"] == pytest.approx(result.plate_top_laser_y_mm)
	assert (tmp_path / "flying_samples.jsonl").exists()
