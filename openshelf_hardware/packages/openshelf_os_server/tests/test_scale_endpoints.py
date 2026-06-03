from types import SimpleNamespace
import pytest
from httpx import AsyncClient
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException
from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
from openshelf_os_server.routers import robot as robot_router


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


def _mock_worker():
	worker = AsyncMock()
	worker.get_scale_data = AsyncMock()
	worker.get_scale_calibration_status = AsyncMock()
	worker.get_scale_calibration_params = AsyncMock()
	worker.set_scale_calibration_params = AsyncMock()
	worker.calibrate_scale_zero = AsyncMock()
	worker.calibrate_scale_weight = AsyncMock()
	worker.get_cached_scale_status = MagicMock(return_value=None)
	return worker


@pytest.mark.anyio
async def test_get_scale_reading(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.get_scale_data.return_value = {
		"weight": -245.7,
		"decimal_position": 1,
		"status": {"stable": True, "zero": False, "overload": False, "valid": True, "raw": 17},
		"raw_registers": {"weight_hi": 0xFFFF, "weight_lo": 0xF667, "decimals": 1, "status": 17},
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.get("/api/robot/debug/scale")

	assert response.status_code == 200
	data = response.json()
	assert data["weight_grams"] == pytest.approx(245.7)
	assert data["decimal_position"] == 1
	assert data["status"]["stable"] is True
	assert data["status"]["valid"] is True


@pytest.mark.anyio
async def test_get_scale_reading_robot_disabled(client: AsyncClient) -> None:
	mock_settings = MagicMock()
	mock_settings.robot.enabled = False

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=mock_settings):
		response = await client.get("/api/robot/debug/scale")

	assert response.status_code == 503


@pytest.mark.anyio
async def test_get_scale_calibration_status(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.get_scale_calibration_status.return_value = {
		"zero_ok": True,
		"weight_ok": False,
		"raw": 0x04,
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.get("/api/robot/debug/scale/calibration/status")

	assert response.status_code == 200
	data = response.json()
	assert data["zero_ok"] is True
	assert data["weight_ok"] is False
	assert data["raw"] == 0x04


@pytest.mark.anyio
async def test_get_scale_calibration_params(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.get_scale_calibration_params.return_value = {
		"full_scale": 6000,
		"calibration_weight": 500,
		"precision": 1,
		"division": 1,
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.get("/api/robot/debug/scale/calibration/params")

	assert response.status_code == 200
	data = response.json()
	assert data["full_scale"] == 6000
	assert data["calibration_weight"] == 500
	assert data["precision"] == 1
	assert data["division"] == 1


@pytest.mark.anyio
async def test_set_scale_calibration_params(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.set_scale_calibration_params.return_value = True

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/scale/calibration/params",
				json={
					"full_scale": 6000,
					"calibration_weight": 500,
					"precision": 1,
					"division": 1,
				},
			)

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is True


@pytest.mark.anyio
async def test_set_scale_calibration_params_validation(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/scale/calibration/params",
				json={
					"full_scale": 6000,
				},
			)

	assert response.status_code == 422


@pytest.mark.anyio
async def test_set_scale_calibration_params_precision_validation(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/scale/calibration/params",
				json={
					"full_scale": 6000,
					"calibration_weight": 500,
					"precision": 5,
				},
			)

	assert response.status_code == 422


@pytest.mark.anyio
async def test_calibrate_scale_zero_success(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.calibrate_scale_zero.return_value = {
		"success": True,
		"status": {"zero_ok": True, "weight_ok": False, "raw": 0x04},
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/scale/calibration/zero")

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is True
	assert data["status"]["zero_ok"] is True


@pytest.mark.anyio
async def test_calibrate_scale_zero_failure(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.calibrate_scale_zero.return_value = {
		"success": False,
		"error": "Zero calibration timeout",
		"status": {"zero_ok": False, "weight_ok": False, "raw": 0},
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/scale/calibration/zero")

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is False
	assert "timeout" in data["error"].lower()


@pytest.mark.anyio
async def test_calibrate_scale_weight_success(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.calibrate_scale_weight.return_value = {
		"success": True,
		"status": {"zero_ok": True, "weight_ok": True, "raw": 0x0C},
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/scale/calibration/weight")

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is True
	assert data["status"]["weight_ok"] is True


@pytest.mark.anyio
async def test_calibrate_scale_weight_failure(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.calibrate_scale_weight.return_value = {
		"success": False,
		"error": "Weight calibration timeout",
		"status": {"zero_ok": True, "weight_ok": False, "raw": 0x04},
	}

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/scale/calibration/weight")

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is False
	assert "timeout" in data["error"].lower()


@pytest.mark.anyio
async def test_scale_endpoint_service_unavailable(client: AsyncClient) -> None:
	mock_worker = _mock_worker()
	mock_worker.get_scale_data.side_effect = Exception("Scale not connected")

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.get("/api/robot/debug/scale")

	assert response.status_code == 503
	data = response.json()
	assert "Scale not connected" in data["detail"]


@pytest.mark.anyio
async def test_retrieve_command_uses_cached_occupied_scale_status() -> None:
	worker = _mock_worker()
	worker.get_cached_scale_status.return_value = SimpleNamespace(occupied=True)
	db = AsyncMock()

	with patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)):
		with pytest.raises(HTTPException) as exc_info:
			await robot_router.robot_commands(
				command=RetrieveItemCommand(location={"side": "left_module", "shelf_idx": 0, "cell_idx": 0}),
				worker=worker,
				current_user=SimpleNamespace(id=1),
				db=db,
			)

	assert exc_info.value.status_code == 409
	worker.get_scale_data.assert_not_awaited()


@pytest.mark.anyio
async def test_store_command_rejects_already_stored_item() -> None:
	worker = _mock_worker()
	item = SimpleNamespace(id=42, upca="123456789012", name="Stored", dimensions={"length": 1.0, "width": 1.0, "height": 1.0}, weight=None)
	location = SimpleNamespace(item_id=42, side="right_module", shelf_idx=1, cell_idx=0)
	item_result = MagicMock()
	item_result.scalar_one_or_none.return_value = item
	location_scalars = MagicMock()
	location_scalars.first.return_value = location
	location_result = MagicMock()
	location_result.scalars.return_value = location_scalars
	db = AsyncMock()
	db.execute.side_effect = [item_result, location_result]

	with patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)):
		with pytest.raises(HTTPException) as exc_info:
			await robot_router.robot_commands(
				command=StoreItemCommand(
					item={
						"upca": "123456789012",
						"dimensions": {"length": 1.0, "width": 1.0, "height": 1.0},
					},
				),
				worker=worker,
				current_user=SimpleNamespace(id=1),
				db=db,
			)

	assert exc_info.value.status_code == 409
	worker.enqueue_command.assert_not_awaited()
