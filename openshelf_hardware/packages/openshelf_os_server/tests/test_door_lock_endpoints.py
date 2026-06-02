from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _closed_doors():
	return {
		"top_closed": True,
		"bottom_closed": True,
		"closed": True,
		"top_open": False,
		"bottom_open": False,
		"open": False,
	}


def _open_doors():
	return {
		"top_closed": False,
		"bottom_closed": False,
		"closed": False,
		"top_open": True,
		"bottom_open": True,
		"open": True,
	}


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


def _mock_worker():
	worker = MagicMock()
	worker._current_command = None
	worker._demo_cycling = False
	worker.is_safety_blocking = MagicMock(return_value=False)
	worker._robot = MagicMock()
	worker._robot._cabinet = MagicMock()
	worker._robot._cabinet.get_status = MagicMock(return_value={"doors": _closed_doors()})
	worker._robot._cabinet.lock = AsyncMock(return_value=_closed_doors())
	worker._robot._cabinet.unlock = AsyncMock(return_value=_open_doors())
	return worker


@pytest.mark.anyio
async def test_get_cabinet_status(client: AsyncClient) -> None:
	worker = _mock_worker()

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.get("/api/cabinet/status")

	assert response.status_code == 200
	assert response.json() == {"doors": _closed_doors()}
	worker._robot._cabinet.get_status.assert_called_once()


@pytest.mark.anyio
async def test_lock_cabinet(client: AsyncClient) -> None:
	worker = _mock_worker()

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.post("/api/cabinet/lock?action=lock")

	assert response.status_code == 200
	assert response.json() == {
		"success": True,
		"action": "lock",
		"doors": _closed_doors(),
	}
	worker._robot._cabinet.lock.assert_awaited_once()
	worker._robot._cabinet.unlock.assert_not_awaited()


@pytest.mark.anyio
async def test_unlock_cabinet(client: AsyncClient) -> None:
	worker = _mock_worker()

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.post("/api/cabinet/lock?action=unlock")

	assert response.status_code == 200
	assert response.json() == {
		"success": True,
		"action": "unlock",
		"doors": _open_doors(),
	}
	worker._robot._cabinet.unlock.assert_awaited_once()
	worker._robot._cabinet.lock.assert_not_awaited()


@pytest.mark.anyio
async def test_unlock_cabinet_rejects_safety_interruption(client: AsyncClient) -> None:
	worker = _mock_worker()
	worker._safety_motion_blocked = True

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.post("/api/cabinet/lock?action=unlock")

	assert response.status_code == 409
	assert response.json()["detail"]["reason"] == "safety_interruption"
	worker._robot._cabinet.unlock.assert_not_awaited()
