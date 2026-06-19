from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


@pytest.mark.anyio
async def test_acknowledge_safety_guard(client: AsyncClient) -> None:
	worker = MagicMock()
	worker.acknowledge_safety_guard = AsyncMock(return_value={"accepted": True, "safety": {}})

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
			response = await client.post("/api/robot/safety/acknowledge")

	assert response.status_code == 200
	assert response.json() == {"message": "success"}
	worker.acknowledge_safety_guard.assert_awaited_once()


@pytest.mark.anyio
async def test_acknowledge_safety_guard_rejects_active_sources(client: AsyncClient) -> None:
	worker = MagicMock()
	worker.acknowledge_safety_guard = AsyncMock(
		return_value={
			"accepted": False,
			"safety": {"active_sources": ["cabinet_doors"]},
		}
	)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
			response = await client.post("/api/robot/safety/acknowledge")

	assert response.status_code == 409
	assert response.json()["detail"] == {
		"message": "Cannot acknowledge: safety interruption is still active",
		"code": "safety_interruption_active",
		"active_sources": ["cabinet_doors"],
	}


@pytest.mark.anyio
async def test_override_safety_guard(client: AsyncClient) -> None:
	worker = MagicMock()
	worker.set_safety_guard_bypassed = AsyncMock(
		return_value={
			"active": False,
			"motion_blocked": False,
			"awaiting_ack": False,
			"can_acknowledge": False,
			"active_sources": [],
			"sources": {},
			"interruption_id": 0,
			"last_changed_at": None,
			"bypassed": True,
		}
	)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
			response = await client.post("/api/robot/safety/override", json={"bypass": True})

	assert response.status_code == 200
	assert response.json()["bypassed"] is True
	worker.set_safety_guard_bypassed.assert_awaited_once_with(True)
