from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


def _mock_worker():
	worker = MagicMock()
	worker._current_command = None
	worker._demo_cycling = False
	worker._robot = MagicMock()
	worker._robot._cabinet = MagicMock()
	worker._robot._cabinet.get_status = MagicMock(
		return_value={"doors": {"top_closed": True, "bottom_closed": True, "closed": True}}
	)
	worker._robot._cabinet.lock = AsyncMock(
		return_value={"top_closed": True, "bottom_closed": True, "closed": True}
	)
	worker._robot._cabinet.unlock = AsyncMock(
		return_value={"top_closed": False, "bottom_closed": False, "closed": False}
	)
	return worker


@pytest.mark.anyio
async def test_get_cabinet_status(client: AsyncClient) -> None:
	worker = _mock_worker()

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.get("/api/cabinet/status")

	assert response.status_code == 200
	assert response.json() == {
		"doors": {"top_closed": True, "bottom_closed": True, "closed": True}
	}
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
		"doors": {"top_closed": True, "bottom_closed": True, "closed": True},
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
		"doors": {"top_closed": False, "bottom_closed": False, "closed": False},
	}
	worker._robot._cabinet.unlock.assert_awaited_once()
	worker._robot._cabinet.lock.assert_not_awaited()
