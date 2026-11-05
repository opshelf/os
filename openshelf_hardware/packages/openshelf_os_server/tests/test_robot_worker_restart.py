"""POST /api/robot/stop releases the robot hardware. A following /start used to run the worker on the
closed GPIO manager (e-stop unreadable, latched emergency stop), and the service shutdown then
stopped motors on the closed bus; /start must refuse and the second shutdown must be a no-op."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from hardware.gpio.manager import GpioManager
from openshelf_os.robot.worker import RobotWorker


class GpioRobot:
	def __init__(self) -> None:
		self._gpio = GpioManager.from_config(
			{"pins": {"estop": {"board": "BCM", "pin": 17, "direction": "input", "inverted": True}}}, fake=True
		)
		self.shutdown_calls = 0

	async def shutdown(self) -> None:
		self.shutdown_calls += 1
		if self._gpio._closed is True:
			raise RuntimeError("Not connected to Modbus device")
		self._gpio.close()

	async def get_status(self) -> None:
		self._gpio.input("estop").read()

	async def handle_command(self, command) -> None:
		pass

	def block_motion_for_safety(self) -> None:
		pass

	def release_motion_safety(self) -> None:
		pass

	def clear_operation_context(self) -> None:
		pass

	async def start_weight_guard(self) -> None:
		pass

	async def stop_weight_guard(self) -> None:
		pass


@pytest.fixture
async def worker():
	RobotWorker._instance = None
	worker = RobotWorker(GpioRobot())
	yield worker
	RobotWorker._instance = None


async def test_start_after_stop_is_refused_and_shutdown_runs_once(client: AsyncClient, worker: RobotWorker) -> None:
	settings = MagicMock()
	settings.robot.enabled = True
	await worker.start()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=settings):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
			stopped = await client.post("/api/robot/stop")
			started = await client.post("/api/robot/start")

	assert stopped.status_code == 200
	assert started.status_code == 409
	assert started.json()["detail"]["code"] == "robot_restart_required"
	assert worker._running is False
	assert worker._is_emergency_stopped is False

	reported = []
	with patch.object(worker, "_report_error", lambda error, exc=None: reported.append(error)):
		await worker.shutdown()

	assert worker._robot.shutdown_calls == 1
	assert reported == []


async def test_cancelled_stop_still_releases_hardware_once(client: AsyncClient, worker: RobotWorker) -> None:
	settings = MagicMock()
	settings.robot.enabled = True
	await worker.start()

	entered = asyncio.Event()
	release = asyncio.Event()

	async def blocked_stop_weight_guard() -> None:
		entered.set()
		await release.wait()

	with patch.object(worker._robot, "stop_weight_guard", blocked_stop_weight_guard):
		stop_request = asyncio.create_task(worker.shutdown())
		await entered.wait()

		with patch("openshelf_os_server.routers.robot.get_settings", return_value=settings):
			with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
				started = await client.post("/api/robot/start")
		with pytest.raises(RuntimeError):
			await worker.start()

		# The server shutdown cancels the in-flight /stop request, then the lifespan shuts down.
		stop_request.cancel()
		with pytest.raises(asyncio.CancelledError):
			await stop_request
		lifespan_shutdown = asyncio.create_task(worker.shutdown())
		await asyncio.sleep(0)
		assert worker._robot.shutdown_calls == 0
		release.set()
		await lifespan_shutdown

	assert started.status_code == 409
	assert started.json()["detail"]["code"] == "robot_restart_required"
	assert worker._robot.shutdown_calls == 1
	assert worker._robot._gpio._closed is True
	assert worker._running is False
