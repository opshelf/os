"""`GET /api/health/cameras` is public, so it must not widen what `GET /api/camera` guards.

On-device tooling (`opshelf-cameras status`) holds no user token. The health route gives it
each camera's `online`/`has_frame` and nothing else: stream state and consumer counts stay
behind the token-gated list.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from openshelf_os_server import main as main_module


async def test_camera_health_is_public_and_exposes_only_stream_flags(unauthenticated_client: AsyncClient) -> None:
	cameras = [
		{"id": "tool", "type": "ToolCamera", "online": True, "running": True, "consumers": 2, "has_frame": True},
		{"id": "overview_one", "type": "OverviewCamera", "online": False, "running": False, "consumers": 0, "has_frame": False},
	]
	with patch.object(main_module, "get_all_cameras_status", AsyncMock(return_value=cameras)):
		health = await unauthenticated_client.get("/api/health/cameras")
		guarded = await unauthenticated_client.get("/api/camera")

	assert health.status_code == 200
	assert health.json() == {
		"cameras": [
			{"id": "tool", "online": True, "has_frame": True},
			{"id": "overview_one", "online": False, "has_frame": False},
		]
	}
	assert guarded.status_code == 401
