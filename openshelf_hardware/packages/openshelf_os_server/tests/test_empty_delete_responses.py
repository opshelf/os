from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from openshelf_os_server.main import app
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.routers import storage as storage_router


@pytest.mark.anyio
async def test_grid_deletes_return_empty_204(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
	grid = type("Grid", (), {"remove_shelf": lambda *_: None, "remove_cell": lambda *_: None})()
	monkeypatch.setattr(storage_router, "_get_robot_grid", lambda: grid)

	shelf_response = await client.delete(
		"/api/storage/grid/shelf?side=left_module&shelf_idx=0"
	)
	cell_response = await client.delete(
		"/api/storage/grid/cell?side=left_module&shelf_idx=0&cell_idx=0"
	)

	assert shelf_response.status_code == 204
	assert shelf_response.content == b""
	assert cell_response.status_code == 204
	assert cell_response.content == b""


@pytest.mark.anyio
async def test_robot_command_delete_is_empty_while_legacy_post_returns_success(
	client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
	worker = type("Worker", (), {"clear_commands": AsyncMock()})()

	async def robot_enabled() -> None:
		return None

	async def get_worker() -> object:
		return worker

	app.dependency_overrides[robot_router.check_robot_enabled] = robot_enabled
	app.dependency_overrides[robot_router.get_robot_worker_or_error] = get_worker
	monkeypatch.setattr(robot_router, "is_laser_auto_calibration_running", AsyncMock(return_value=False))
	try:
		delete_response = await client.delete("/api/robot/commands")
		legacy_response = await client.post("/api/robot/clear_commands")
	finally:
		app.dependency_overrides.pop(robot_router.check_robot_enabled, None)
		app.dependency_overrides.pop(robot_router.get_robot_worker_or_error, None)

	assert delete_response.status_code == 204
	assert delete_response.content == b""
	assert legacy_response.status_code == 200
	assert legacy_response.json() == {"message": "success"}
	assert worker.clear_commands.await_count == 2
