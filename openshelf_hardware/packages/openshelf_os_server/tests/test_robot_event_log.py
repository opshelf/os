from __future__ import annotations


import pytest
from httpx import AsyncClient

from openshelf_os_server.core.event_log import append_robot_event


@pytest.mark.anyio
async def test_event_log_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/robot/events/log")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_legacy_failure_debug_never_reaches_a_client(client: AsyncClient) -> None:
    """Failure events persisted before `debug` was removed still carry the exception
    chain; the log has no retention, so the replay endpoint must drop it."""
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

    response = await client.get("/api/robot/events/log", params={"since": seq - 1})

    assert response.status_code == 200
    assert "motor 1" not in response.text
    failure = next(event["payload"] for event in response.json()["events"] if event["seq"] == seq)
    assert "debug" not in failure
    assert failure["fault_code"] == "motor_alarm"
