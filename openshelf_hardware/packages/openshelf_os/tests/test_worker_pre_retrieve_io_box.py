"""A queued retrieval must not deliver into an IO box an earlier command already filled."""
from __future__ import annotations

import asyncio
import json

import pytest

from openshelf_os.robot.commands import DeliverTrayCommand, RetrieveItemCommand
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import RobotWorker

LOCATION = {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}


class ScaleRobot:
	def __init__(self, weight: float | None, *, fails: bool = False) -> None:
		self.weight = weight
		self.fails = fails
		self.handled_commands: list[object] = []

	def block_motion_for_safety(self) -> None:
		pass

	def release_motion_safety(self) -> None:
		pass

	async def get_status(self) -> None:
		return None

	async def get_scale_data(self) -> dict:
		if self.fails is True:
			raise RuntimeError("scale offline")
		return {"weight": self.weight, "status": {"stable": True, "valid": True}}

	async def handle_command(self, command: object) -> RobotCommandSuccessful:
		self.handled_commands.append(command)
		return RobotCommandSuccessful(command="retrieve_item", barcode="880000000001")


async def _run_one(robot: ScaleRobot, command: object, *, bypassed: bool = False) -> dict:
	RobotWorker._instance = None
	worker = RobotWorker(robot)  # type: ignore[arg-type]
	worker._safety_guard_bypassed = bypassed
	events = await worker.subscribe_events()
	await worker.enqueue_command(command, command_id="00000000-0000-4000-8000-000000000001")
	worker._running = True
	task = asyncio.create_task(worker.process_commands())
	try:
		while True:
			event = json.loads(await asyncio.wait_for(events.get(), timeout=2))
			if event.get("command") == "retrieve_item":
				return event
	finally:
		worker._running = False
		task.cancel()
		with pytest.raises(asyncio.CancelledError):
			await task
		RobotWorker._instance = None


@pytest.mark.asyncio
async def test_occupied_io_box_fails_the_retrieve_without_motion() -> None:
	robot = ScaleRobot(weight=350.0)

	event = await _run_one(robot, RetrieveItemCommand(barcode="880000000001", location=LOCATION))

	assert event["message"] == "failed"
	assert event["code"] == "io_box_occupied"
	assert event["barcode"] == "880000000001"
	assert robot.handled_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("robot,bypassed", [
	(ScaleRobot(weight=0.4), False),
	(ScaleRobot(weight=None, fails=True), False),
	(ScaleRobot(weight=350.0), True),
])
async def test_retrieve_runs_when_box_is_empty_unreadable_or_guard_bypassed(robot: ScaleRobot, bypassed: bool) -> None:
	event = await _run_one(robot, RetrieveItemCommand(barcode="880000000001", location=LOCATION), bypassed=bypassed)

	assert event["message"] == "success"
	assert len(robot.handled_commands) == 1


@pytest.mark.asyncio
async def test_tray_delivery_is_left_to_the_bin_workflow() -> None:
	robot = ScaleRobot(weight=350.0)

	event = await _run_one(robot, DeliverTrayCommand(barcode="880000000001", location=LOCATION))

	assert event["message"] == "success"
	assert len(robot.handled_commands) == 1
