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


def _engaged_latches():
	return {
		"top_released": False,
		"bottom_released": False,
		"released": False,
		"top_engaged": True,
		"bottom_engaged": True,
		"engaged": True,
	}


def _disengaged_latches():
	return {
		"top_released": False,
		"bottom_released": False,
		"released": False,
		"top_engaged": False,
		"bottom_engaged": False,
		"engaged": False,
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
	worker._robot._cabinet.is_configured = MagicMock(return_value=True)
	worker._robot._cabinet.get_status = MagicMock(
		return_value={"doors": _closed_doors(), "latches": _engaged_latches()}
	)
	worker._robot._cabinet.set_passive = AsyncMock(
		return_value={"doors": _closed_doors(), "latches": _engaged_latches()}
	)
	worker._robot._cabinet.release = AsyncMock(
		return_value={"doors": _open_doors(), "latches": _disengaged_latches()}
	)
	return worker


@pytest.mark.anyio
async def test_release_cabinet_latches_rejects_safety_interruption(client: AsyncClient) -> None:
	worker = _mock_worker()
	worker._safety_motion_blocked = True

	with patch("openshelf_os_server.routers.cabinet.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.cabinet.get_robot_worker", return_value=worker):
			response = await client.post("/api/cabinet/release?action=release")

	assert response.status_code == 409
	assert response.json()["detail"]["reason"] == "safety_interruption"
	worker._robot._cabinet.release.assert_not_awaited()
