from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(robot=SimpleNamespace(enabled=True))


def _worker() -> AsyncMock:
    worker = AsyncMock()
    worker.enqueue_command = AsyncMock()
    return worker


@pytest.mark.anyio
async def test_store_item_observer_capture_does_not_mutate_command_payload(client: AsyncClient) -> None:
    worker = _worker()
    payload = {
        "command": "store_item",
        "item": {
            "upca": "123456789012",
            "name": "Test item",
            "dimensions": {"width": 1, "height": 2, "length": 3},
        },
    }

    with patch("openshelf_os_server.routers.robot.get_settings", return_value=_settings()):
        with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
            with patch(
                "openshelf_os_server.routers.robot.capture_store_observer_measurement",
                AsyncMock(),
            ) as observer:
                response = await client.post("/api/robot/commands", json=payload)

    assert response.status_code == 200
    queued_command = worker.enqueue_command.await_args.args[0]
    assert queued_command.item == payload["item"]
    observer.assert_awaited_once()
    assert observer.await_args.kwargs["human_dimensions"] == payload["item"]["dimensions"]
    assert observer.await_args.kwargs["human_dimensions_source"] == "command_payload"


@pytest.mark.anyio
async def test_store_item_continues_when_observer_capture_raises(client: AsyncClient) -> None:
    worker = _worker()
    payload = {
        "command": "store_item",
        "item": {"dimensions": {"width": 4, "height": 5, "length": 6}},
    }

    with patch("openshelf_os_server.routers.robot.get_settings", return_value=_settings()):
        with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
            with patch(
                "openshelf_os_server.routers.robot.capture_store_observer_measurement",
                AsyncMock(side_effect=RuntimeError("observer failed")),
            ):
                response = await client.post("/api/robot/commands", json=payload)

    assert response.status_code == 200
    queued_command = worker.enqueue_command.await_args.args[0]
    assert queued_command.item == payload["item"]
