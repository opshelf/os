import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from httpx import AsyncClient

from openshelf_os_server.core.camera import (
	camera_registry,
	CameraStreamManager,
	stop_camera_stream_by_id,
)


@pytest.mark.anyio
async def test_list_cameras_empty(client: AsyncClient) -> None:
	with patch.dict(camera_registry, {}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert "cameras" in data
	assert data["cameras"] == []


@pytest.mark.anyio
async def test_list_cameras_with_sizer_camera(client: AsyncClient) -> None:
	mock_manager = MagicMock(spec=CameraStreamManager)
	mock_manager.is_running = True
	mock_manager.consumer_count = 2
	mock_manager.latest_frame = b"fake_frame"
	mock_manager.camera_type = "IOBoxSizerCamera"

	with patch.dict(camera_registry, {"iobox_sizer": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 1

	camera = data["cameras"][0]
	assert camera["id"] == "iobox_sizer"
	assert camera["type"] == "IOBoxSizerCamera"
	assert camera["running"] is True
	assert camera["consumers"] == 2
	assert camera["has_frame"] is True


@pytest.mark.anyio
async def test_stream_camera_not_found(client: AsyncClient) -> None:
	with patch.dict(camera_registry, {}, clear=True):
		response = await client.get("/api/camera/nonexistent")

	assert response.status_code == 404
	data = response.json()
	assert "not found" in data["detail"].lower()


@pytest.mark.anyio
async def test_stream_camera_invalid_id(client: AsyncClient) -> None:
	mock_sizer = MagicMock(spec=CameraStreamManager)

	with patch.dict(camera_registry, {"iobox_sizer": mock_sizer}, clear=True):
		response = await client.get("/api/camera/invalid_camera")

	assert response.status_code == 404


@pytest.mark.anyio
async def test_camera_types_identified_correctly(client: AsyncClient) -> None:
	sizer_manager = MagicMock(spec=CameraStreamManager)
	sizer_manager.is_running = False
	sizer_manager.consumer_count = 0
	sizer_manager.latest_frame = None
	sizer_manager.camera_type = "IOBoxSizerCamera"

	with patch.dict(camera_registry, {"iobox_sizer": sizer_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()

	sizer_camera = next((c for c in data["cameras"] if c["id"] == "iobox_sizer"), None)
	assert sizer_camera is not None
	assert sizer_camera["type"] == "IOBoxSizerCamera"


@pytest.mark.anyio
async def test_opencv_stream_manager_always_calls_capture_raw() -> None:
	mock_cam = MagicMock()
	mock_cam.capture_raw.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
	manager = CameraStreamManager(camera=mock_cam)
	await manager.start()
	await asyncio.sleep(0.2)
	await manager.stop()
	mock_cam.capture_raw.assert_called()


@pytest.mark.anyio
async def test_stop_camera_stream_by_id_stops_manager_when_last_consumer_disconnects() -> None:
	manager = MagicMock(spec=CameraStreamManager)
	manager.is_running = True
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"iobox_sizer": manager}, clear=True):
		await stop_camera_stream_by_id("iobox_sizer")

	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_stop_camera_stream_by_id_keeps_manager_running_with_remaining_consumers() -> None:
	manager = MagicMock(spec=CameraStreamManager)
	manager.is_running = True
	manager.decrement_consumers = MagicMock(return_value=1)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"iobox_sizer": manager}, clear=True):
		await stop_camera_stream_by_id("iobox_sizer")

	manager.stop.assert_not_awaited()
