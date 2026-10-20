import asyncio

import pytest
from httpx import AsyncClient
from openshelf_os_server.routers import robot as robot_router
from openshelf_os.exceptions import MotorError
from unittest.mock import MagicMock, AsyncMock, patch


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


def _mock_worker(power_cycle: AsyncMock | None = None):
	worker = AsyncMock()
	worker.set_needs_home = MagicMock()
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker._current_command = None
	worker._demo_cycling = False
	worker._is_crashed = False
	worker._crash_reason = None

	robot = MagicMock()
	robot.get_power_rail_states = MagicMock(
		return_value={"pwr_out0": True, "pwr_out1": False, "pwr_out2": None, "pwr_out3": True}
	)

	if power_cycle is None:
		async def power_cycle(_rail: str, on_cut=None):
			if on_cut is not None:
				on_cut()
			return ("x_axis", "y_axis", "z_axis", "stage", "rotate")
		robot.power_cycle_rail = AsyncMock(side_effect=power_cycle)
	else:
		robot.power_cycle_rail = power_cycle

	worker._robot = robot

	return worker


@pytest.mark.anyio
async def test_power_cycle_request_cancellation_marks_needs_home() -> None:
	called = asyncio.Event()
	mock_worker = _mock_worker()

	async def _cut_and_hold(rail: str, on_cut=None):
		if on_cut is not None:
			on_cut()
		called.set()
		await asyncio.sleep(10)
		return ("x_axis", "y_axis", "z_axis", "stage", "rotate")

	mock_worker._robot.power_cycle_rail = AsyncMock(side_effect=_cut_and_hold)

	task = asyncio.create_task(
		robot_router.power_cycle_rail("pwr_out0", worker=mock_worker)
	)
	await called.wait()
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task

	assert called.is_set()
	mock_worker.set_needs_home.assert_called_once_with(
		"power cycle: pwr_out0", ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	)


@pytest.mark.anyio
async def test_power_cycle_reconfigure_failure_is_503_and_marks_axes_for_homing(client: AsyncClient) -> None:
	async def fail_after_cut(rail: str, on_cut=None):
		if on_cut is not None:
			on_cut()
		raise MotorError("Power cycle reconfigure failed for axes: x_axis")

	mock_worker = _mock_worker(power_cycle=AsyncMock(side_effect=fail_after_cut))

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/power/pwr_out0/cycle")

	assert response.status_code == 503
	assert response.json()["detail"]["code"] == "power_cycle_failed"
	assert response.json()["detail"]["message"] == "The cabinet could not power cycle that part. Try again."
	mock_worker.set_needs_home.assert_called_once_with(
		"power cycle: pwr_out0", ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	)
