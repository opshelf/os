from __future__ import annotations


import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_event_log_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/robot/events/log")
    assert response.status_code == 401
