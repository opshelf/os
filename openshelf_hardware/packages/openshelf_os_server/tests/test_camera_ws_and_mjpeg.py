from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.main import app
from openshelf_os_server.routers.camera import _mjpeg_frame_thumbnail
from openshelf_os_server.settings import get_settings


def _make_ws_token() -> str:
	settings = get_settings()
	auth = AuthService(
		settings.auth.jwt_secret, settings.auth.jwt_algorithm
	)
	return auth.create_access_token(
		user_id=1,
		email="admin@opshelf.com",
		role="admin",
		permissions=["users:read", "users:write", "robot:control"],
		access_level=None,
		expires_minutes=60,
	)


def test_camera_ws_rejects_unknown_camera() -> None:
	client = TestClient(app, raise_server_exceptions=False)
	with pytest.raises(WebSocketDisconnect) as exc_info:
		with client.websocket_connect(
			f"/api/camera/ws/missing?token={_make_ws_token()}"
		) as websocket:
			websocket.receive_text()
	assert exc_info.value.code == 4404


def test_mjpeg_thumbnail_rejects_garbage() -> None:
	assert _mjpeg_frame_thumbnail(b"not a jpeg") is None


