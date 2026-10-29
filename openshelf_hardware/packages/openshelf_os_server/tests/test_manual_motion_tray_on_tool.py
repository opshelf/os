"""The manual motion guard reads the gripper itself: a flow outside the command queue can leave a tray held."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from openshelf_os.robot.worker import TRAY_ON_TOOL_CODE, RobotWorker
from openshelf_os_server.core.manual_motion import manual_motion_guard


class GripperRobot:
	def __init__(self, held: bool) -> None:
		self.held = held
		self.reads = 0

	async def read_tray_held(self) -> bool:
		self.reads += 1
		return self.held

	async def get_status(self) -> None:
		return None


@pytest.fixture
async def worker_factory():
	async def make(robot: GripperRobot) -> RobotWorker:
		RobotWorker._instance = None
		return RobotWorker(robot)

	yield make
	RobotWorker._instance = None


async def test_stale_false_cache_with_a_held_gripper_blocks_manual_motion(worker_factory):
	robot = GripperRobot(held=True)
	worker = await worker_factory(robot)
	assert worker.tray_on_tool() is False

	entered = False
	with pytest.raises(HTTPException) as exc_info:
		async with manual_motion_guard("motor_move:x_axis", worker=worker):
			entered = True

	assert entered is False
	assert exc_info.value.status_code == 409
	assert exc_info.value.detail["code"] == TRAY_ON_TOOL_CODE
	assert worker.tray_on_tool() is True
	assert json.loads(worker._latest_status_payload)["tray_on_tool"] is True


async def test_empty_gripper_admits_manual_motion_and_reads_it(worker_factory):
	robot = GripperRobot(held=False)
	worker = await worker_factory(robot)

	async with manual_motion_guard("motor_move:x_axis", worker=worker):
		pass

	assert robot.reads == 1
	assert worker.tray_on_tool() is False


async def test_allowed_operations_do_not_read_the_gripper(worker_factory):
	robot = GripperRobot(held=True)
	worker = await worker_factory(robot)

	async with manual_motion_guard("gripper:open", worker=worker, allow_tray_on_tool=True):
		pass

	assert robot.reads == 0
	assert worker.tray_on_tool() is False
