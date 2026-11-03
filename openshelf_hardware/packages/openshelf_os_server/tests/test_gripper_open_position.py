"""Setting the gripper open position from the reference cells' hole stops."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleSide,
)
from openshelf_os_server.core.laser_auto_calibration import gripper_runner

RUNNER = "openshelf_os_server.core.laser_auto_calibration.gripper_runner"


def _datum_grid() -> StorageGrid:
	grid = StorageGrid(filename="storage.json")

	def datum(s: int) -> list[Shelf]:
		cell = Cell(
			cell_idx=0,
			status=CellStatus.OCCUPIED,
			item=None,
			calibration_point=CalibrationCoordinate(x=354.0, y=41.0, s=s, z_offset=12.0),
			dimensions=Dimensions(length=400.0, width=26.0, height=30.0),
			is_manual=True,
		)
		return [Shelf(shelf_idx=0, cells=[cell])]

	grid.front_module = datum(0)
	grid.back_module = datum(1)
	return grid


class FakeDrive:
	"""A tray hole per side: splayed at a target at least `contact_margin` counts past its stop, the
	fingers stop there and report contact; nearer, they stop at the target and report none."""

	def __init__(self, stops: dict[int, int], contact_margin: int = 6) -> None:
		self.stops = stops
		self.contact_margin = contact_margin
		self.open_position = 0x20
		self.s = 0

	async def set_open_position(self, value: int) -> None:
		self.open_position = value

	async def open(self):
		stop = self.stops[self.s]
		contact = self.open_position <= stop - self.contact_margin
		return SimpleNamespace(gripped=contact, position=stop if contact else self.open_position)

	async def close(self):
		return None

	async def safe_open(self):
		return None


class FakeTool:
	def __init__(self, drive: FakeDrive) -> None:
		self._gs = drive

	async def move(self, coordinate) -> None:
		self._gs.s = int(coordinate.s)

	async def rotate_to_position(self, _position) -> None:
		pass

	async def extend_empty(self, _coordinate) -> None:
		pass

	async def release(self, _phase: str) -> None:
		pass

	async def retract_empty(self) -> None:
		pass


class FakeConfig:
	def __init__(self) -> None:
		self.robot = SimpleNamespace(tool=SimpleNamespace(gripper=SimpleNamespace(open_position=0x20)))

	def model_copy(self, *, deep: bool):
		return copy.deepcopy(self)


async def _calibrate(drive: FakeDrive, tmp_path):
	worker = SimpleNamespace(_robot=SimpleNamespace(_tool=FakeTool(drive), _grid=_datum_grid(), reload_parameters=MagicMock()))
	save = MagicMock()

	with (
		patch(f"{RUNNER}.get_root_config", return_value=FakeConfig()),
		patch(f"{RUNNER}.save_root_config", save),
		patch(f"{RUNNER}.clear_config_cache"),
		patch(f"{RUNNER}._build_default_scans_dir", return_value=str(tmp_path)),
	):
		result = await gripper_runner.calibrate_gripper_open_position(
			worker, [StorageModuleSide.FRONT, StorageModuleSide.BACK]
		)

	return result, save


@pytest.mark.asyncio
async def test_the_widest_hole_sets_the_one_open_position_and_it_is_saved(tmp_path) -> None:
	"""One drive register serves both sides: the narrower side's target would leave the wider
	side's hole out of reach (lb4a-10: stop 14, a target of 32 never gripped)."""
	drive = FakeDrive(stops={0: 14, 1: 20})

	result, save = await _calibrate(drive, tmp_path)

	assert [sample.stop_position for sample in result.samples] == [14, 20]
	assert result.applied is True
	assert result.open_position == 6
	assert drive.open_position == 6
	assert save.call_args.args[0].robot.tool.gripper.open_position == 6


@pytest.mark.asyncio
async def test_a_target_that_does_not_grip_on_the_check_is_neither_kept_nor_saved(tmp_path) -> None:
	drive = FakeDrive(stops={0: 14, 1: 14}, contact_margin=10)

	result, save = await _calibrate(drive, tmp_path)

	assert result.open_position == 6
	assert result.applied is False
	assert drive.open_position == 0x20
	save.assert_not_called()
