"""Setting the gripper open position from the reference cells' hole stops."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

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

	def __init__(
		self, stops: dict[int, int], contact_margin: int = 6, abort_write_of: int | None = None, open_position: int = 0x20
	) -> None:
		self.abort_write_of = abort_write_of
		self.stops = stops
		self.contact_margin = contact_margin
		self.open_position = open_position
		self.writes: list[int] = []
		self.s = 0

	async def set_open_position(self, value: int) -> None:
		self.writes.append(value)
		self.open_position = value
		if value == self.abort_write_of:
			raise asyncio.CancelledError

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


async def _calibrate(drive: FakeDrive, tmp_path, sides: list[StorageModuleSide] | None = None):
	worker = SimpleNamespace(_robot=SimpleNamespace(_tool=FakeTool(drive), _grid=_datum_grid(), reload_parameters=MagicMock()))
	save = MagicMock()

	with (
		patch(f"{RUNNER}.get_root_config", return_value=FakeConfig()),
		patch(f"{RUNNER}.save_root_config", save),
		patch(f"{RUNNER}.clear_config_cache"),
		patch(f"{RUNNER}._build_default_scans_dir", return_value=str(tmp_path)),
	):
		result = await gripper_runner.calibrate_gripper_open_position(
			worker, sides or [StorageModuleSide.FRONT, StorageModuleSide.BACK]
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
async def test_a_back_only_run_never_narrows_the_value_the_front_hole_needs(tmp_path) -> None:
	"""A both-sides run set 6 for front stop 14; a later back-only stop of 20 gives 12, which would
	leave the front hole with no grip."""
	drive = FakeDrive(stops={0: 14, 1: 20}, open_position=6)

	result, save = await _calibrate(drive, tmp_path, [StorageModuleSide.BACK])

	assert [sample.stop_position for sample in result.samples] == [20]
	assert result.applied is False
	assert result.open_position == 6
	assert drive.open_position == 6
	assert 12 not in drive.writes
	assert drive.writes[-1] == 6
	assert result.skipped == ["open position stays 0x06: front not measured, measured value 0x0C would be narrower"]
	save.assert_not_called()


@pytest.mark.asyncio
async def test_a_back_only_run_may_still_open_further_than_the_previous_value(tmp_path) -> None:
	drive = FakeDrive(stops={0: 14, 1: 10}, open_position=6)

	result, save = await _calibrate(drive, tmp_path, [StorageModuleSide.BACK])

	assert result.applied is True
	assert result.open_position == 2
	assert drive.open_position == 2
	assert save.call_args.args[0].robot.tool.gripper.open_position == 2


@pytest.mark.asyncio
async def test_a_partial_run_may_narrow_when_the_other_side_has_no_reference_cell(tmp_path) -> None:
	"""Only sides that have a reference cell count as unmeasured."""
	drive = FakeDrive(stops={0: 14, 1: 20}, open_position=6)
	worker = SimpleNamespace(_robot=SimpleNamespace(_tool=FakeTool(drive), _grid=_datum_grid(), reload_parameters=MagicMock()))
	worker._robot._grid.front_module = []

	with (
		patch(f"{RUNNER}.get_root_config", return_value=FakeConfig()),
		patch(f"{RUNNER}.save_root_config", MagicMock()),
		patch(f"{RUNNER}.clear_config_cache"),
		patch(f"{RUNNER}._build_default_scans_dir", return_value=str(tmp_path)),
	):
		result = await gripper_runner.calibrate_gripper_open_position(worker, [StorageModuleSide.BACK])

	assert result.applied is True
	assert drive.open_position == 12


@pytest.mark.asyncio
async def test_a_target_that_does_not_grip_on_the_check_is_neither_kept_nor_saved(tmp_path) -> None:
	drive = FakeDrive(stops={0: 14, 1: 14}, contact_margin=10)

	result, save = await _calibrate(drive, tmp_path)

	assert result.open_position == 6
	assert result.applied is False
	assert drive.open_position == 0x20
	save.assert_not_called()


@pytest.mark.asyncio
async def test_an_abort_on_the_new_target_write_puts_the_previous_value_back(tmp_path) -> None:
	"""The write can land on the drive before the abort does; the unverified value must not stay live."""
	drive = FakeDrive(stops={0: 14, 1: 14}, abort_write_of=6)

	with pytest.raises(asyncio.CancelledError):
		await _calibrate(drive, tmp_path)

	assert drive.open_position == 0x20


async def _post_route(client: AsyncClient, drive: FakeDrive, grid: StorageGrid, tmp_path, body: dict):
	tool = FakeTool(drive)
	tool.is_grip_detection_enabled = True
	tool.retract = AsyncMock()
	worker = MagicMock()
	worker.needs_home.return_value = False
	worker.needs_reset.return_value = False
	worker.refresh_tray_on_tool = AsyncMock(return_value=False)
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker._current_command = None
	worker._demo_cycling = False
	worker._safety_guard_bypassed = False
	worker._safety_motion_blocked = False
	worker._awaiting_safety_ack = False
	worker._safety_active_sources = []
	worker._command_queue = SimpleNamespace(qsize=lambda: 0)
	worker._queued_commands = []
	worker._current_operation = None
	worker._robot = SimpleNamespace(_tool=tool, _grid=grid, reload_parameters=MagicMock())
	routes = "openshelf_os_server.routers.calibration"

	with (
		patch(f"{routes}.get_robot_worker", return_value=worker),
		patch(f"{routes}.ensure_laser_auto_calibration_not_running", new=AsyncMock()),
		patch(f"{routes}._clear_motors_for_move", new=AsyncMock()),
		patch(f"{RUNNER}.get_root_config", return_value=FakeConfig()),
		patch(f"{RUNNER}.save_root_config", MagicMock()),
		patch(f"{RUNNER}.clear_config_cache"),
		patch(f"{RUNNER}._build_default_scans_dir", return_value=str(tmp_path)),
	):
		return await client.post("/api/calibration/gripper/open-position", json=body)


@pytest.mark.asyncio
async def test_route_reports_a_side_without_a_reference_cell_and_measures_the_other(
	client: AsyncClient, tmp_path
) -> None:
	grid = _datum_grid()
	grid.back_module = []

	response = await _post_route(client, FakeDrive(stops={0: 14}), grid, tmp_path, {})

	assert response.status_code == 200
	body = response.json()
	assert body["applied"] is True
	assert [sample["stop_position"] for sample in body["samples"]] == [14]
	assert body["skipped"] == ["back: no hand-calibrated reference cell"]


@pytest.mark.asyncio
async def test_route_refuses_when_no_requested_side_has_a_reference_cell(client: AsyncClient, tmp_path) -> None:
	grid = _datum_grid()
	grid.back_module = []

	response = await _post_route(client, FakeDrive(stops={0: 14}), grid, tmp_path, {"side": "back_module"})

	assert response.status_code == 400
	assert response.json()["detail"]["code"] == "reference_cell_missing"
