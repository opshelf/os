from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from openshelf_os.common import ConfigError


def _mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.robot.enabled = True
    return settings


@pytest.mark.anyio
async def test_get_y_axis_limits_returns_logical_states(client: AsyncClient) -> None:
    worker = MagicMock()
    worker.get_y_axis_limit_state.return_value = {
        "minimum_triggered": True,
        "maximum_triggered": False,
    }

    with (
        patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()),
        patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker),
    ):
        response = await client.get("/api/robot/debug/y-axis-limits")

    assert response.status_code == 200
    assert response.json() == {
        "minimum_triggered": True,
        "maximum_triggered": False,
    }


@pytest.mark.anyio
async def test_get_y_axis_limits_reports_unavailable_gpio(client: AsyncClient) -> None:
    worker = MagicMock()
    worker.get_y_axis_limit_state.side_effect = ConfigError("GPIO manager is unavailable")

    with (
        patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()),
        patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker),
    ):
        response = await client.get("/api/robot/debug/y-axis-limits")

    assert response.status_code == 503
    assert response.json()["detail"] == "GPIO manager is unavailable"
