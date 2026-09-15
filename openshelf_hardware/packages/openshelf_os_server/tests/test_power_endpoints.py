import asyncio

import pytest
from httpx import AsyncClient
from openshelf_os_server.routers import robot as robot_router
from openshelf_os.exceptions import MotorError
from unittest.mock import ANY, MagicMock, AsyncMock, patch


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
async def test_list_power_rails(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.get("/api/robot/debug/power")

	assert response.status_code == 200
	data = response.json()
	assert data["hold_seconds"] == 3.0
	rails = {entry["rail"]: entry for entry in data["rails"]}
	assert set(rails) == {"pwr_out0", "pwr_out1", "pwr_out2", "pwr_out3"}
	assert rails["pwr_out0"]["axes"] == ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	assert rails["pwr_out0"]["gripper"] is True
	assert rails["pwr_out1"]["gripper"] is False
	assert rails["pwr_out1"]["supplies"] == "IO box"
	assert rails["pwr_out3"]["axes"] == []
	assert rails["pwr_out0"]["powered"] is True
	assert rails["pwr_out1"]["powered"] is False
	assert rails["pwr_out2"]["powered"] is None


@pytest.mark.anyio
async def test_power_cycle_marks_dropped_axes_for_homing(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/power/pwr_out0/cycle")

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is True
	assert data["rail"] == "pwr_out0"
	assert data["axes_needing_home"] == ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	mock_worker._robot.power_cycle_rail.assert_awaited_once_with("pwr_out0", on_cut=ANY)
	mock_worker.set_needs_home.assert_called_once_with(
		"power cycle: pwr_out0", ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	)


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
async def test_power_cycle_of_a_rail_without_axes_skips_needs_home(client: AsyncClient) -> None:
	mock_worker = _mock_worker(power_cycle=AsyncMock(return_value=()))

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/power/pwr_out2/cycle")

	assert response.status_code == 200
	assert response.json()["axes_needing_home"] == []
	mock_worker.set_needs_home.assert_not_called()


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
	assert "x_axis" in response.json()["detail"]["message"]
	mock_worker.set_needs_home.assert_called_once_with(
		"power cycle: pwr_out0", ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
	)


@pytest.mark.anyio
async def test_power_cycle_unknown_rail_is_404(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/power/pwr_out9/cycle")

	assert response.status_code == 404
	mock_worker._robot.power_cycle_rail.assert_not_awaited()


@pytest.mark.anyio
async def test_power_cycle_failure_is_503(client: AsyncClient) -> None:
	failing = AsyncMock(side_effect=Exception("y_axis is moving"))
	mock_worker = _mock_worker(power_cycle=failing)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/power/pwr_out0/cycle")

	assert response.status_code == 503
	assert response.json()["detail"]["code"] == "power_cycle_failed"
	mock_worker.set_needs_home.assert_not_called()


@pytest.mark.anyio
async def test_power_cycle_robot_disabled(client: AsyncClient) -> None:
	mock_settings = MagicMock()
	mock_settings.robot.enabled = False

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=mock_settings):
		response = await client.post("/api/robot/debug/power/pwr_out0/cycle")

	assert response.status_code == 503
