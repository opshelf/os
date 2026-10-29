"""The manual motion guard reads the gripper itself: a flow outside the command queue can leave a tray held.
The /robot/commands precheck reads it only when the cache says held: a running command carries a tray legitimately."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from openshelf_os.robot.commands import StoreItemCommand
from openshelf_os.robot.worker import TRAY_ON_TOOL_CODE, RobotWorker
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.routers.robot import _ensure_command_allowed_while_tray_on_tool


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


async def test_command_precheck_does_not_read_the_gripper_while_the_cache_is_clear(worker_factory):
	robot = GripperRobot(held=True)
	worker = await worker_factory(robot)

	await _ensure_command_allowed_while_tray_on_tool(StoreItemCommand(item={"barcode": "880000000001"}), worker)

	assert robot.reads == 0
	assert worker.tray_on_tool() is False


async def test_command_precheck_blocks_a_cached_held_tray_and_passes_once_cleared(worker_factory):
	robot = GripperRobot(held=True)
	worker = await worker_factory(robot)
	worker._tray_on_tool = True

	with pytest.raises(HTTPException) as exc_info:
		await _ensure_command_allowed_while_tray_on_tool(StoreItemCommand(item={"barcode": "880000000001"}), worker)

	assert exc_info.value.status_code == 409
	assert exc_info.value.detail["code"] == TRAY_ON_TOOL_CODE
	assert robot.reads == 1

	robot.held = False
	await _ensure_command_allowed_while_tray_on_tool(StoreItemCommand(item={"barcode": "880000000001"}), worker)

	assert robot.reads == 2
	assert worker.tray_on_tool() is False
