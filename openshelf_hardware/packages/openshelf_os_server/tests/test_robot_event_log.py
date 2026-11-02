from __future__ import annotations


import pytest
from httpx import AsyncClient

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.core.event_log import append_robot_event
from openshelf_os_server.settings import DEVELOPER_SYSTEM_USER_EMAIL, get_settings


@pytest.mark.anyio
async def test_event_log_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/robot/events/log")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_failure_debug_reaches_only_the_developer_user(client: AsyncClient) -> None:
    """A failed command event carries the worker's exception as `debug`; the kiosk
    subscribes to the same log with its own token and must never receive it."""
    seq = await append_robot_event(
        {
            "command": "retrieve_item",
            "message": "failed",
            "code": "execution_error",
            "fault_code": "motor_alarm",
            "error": "A motor drive raised an alarm and stopped. Clear the alarm, then reset the robot.",
            "debug": {"chain": ["MotorError: Movement failed for motor 1 (status=FAULT)"]},
        }
    )
    assert seq is not None

    def failure(response) -> dict:
        assert response.status_code == 200
        return next(event["payload"] for event in response.json()["events"] if event["seq"] == seq)

    operator_view = failure(await client.get("/api/robot/events/log", params={"since": seq - 1}))
    assert "debug" not in operator_view
    assert operator_view["fault_code"] == "motor_alarm"

    settings = get_settings()
    token = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm).create_access_token(
        user_id=1, email=DEVELOPER_SYSTEM_USER_EMAIL, role="admin", permissions=[], expires_minutes=5
    )
    developer_view = failure(
        await client.get(
            "/api/robot/events/log",
            params={"since": seq - 1},
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    assert developer_view["debug"]["chain"] == ["MotorError: Movement failed for motor 1 (status=FAULT)"]
