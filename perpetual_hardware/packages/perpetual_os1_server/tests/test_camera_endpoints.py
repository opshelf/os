import pytest
from httpx import AsyncClient
from unittest.mock import patch, MagicMock, AsyncMock

from perpetual_os1_server.core.camera import (
	camera_registry,
	CameraStreamManager,
	ToolCameraStreamManager,
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
async def test_list_cameras_with_io_camera(client: AsyncClient) -> None:
	mock_manager = MagicMock(spec=CameraStreamManager)
	mock_manager.is_running = True
	mock_manager.consumer_count = 2
	mock_manager.latest_frame = b"fake_frame"

	with patch.dict(camera_registry, {"io": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 1

	camera = data["cameras"][0]
	assert camera["id"] == "io"
	assert camera["type"] == "AFSCam"
	assert camera["running"] is True
	assert camera["consumers"] == 2
	assert camera["has_frame"] is True


@pytest.mark.anyio
async def test_list_cameras_with_tool_camera(client: AsyncClient) -> None:
	mock_manager = MagicMock(spec=ToolCameraStreamManager)
	mock_manager.is_running = False
	mock_manager.consumer_count = 0
	mock_manager.latest_frame = None

	with patch.dict(camera_registry, {"tool": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 1

	camera = data["cameras"][0]
	assert camera["id"] == "tool"
	assert camera["type"] == "ToolCamera"
	assert camera["running"] is False
	assert camera["consumers"] == 0
	assert camera["has_frame"] is False


@pytest.mark.anyio
async def test_list_cameras_multiple(client: AsyncClient) -> None:
	mock_io = MagicMock(spec=CameraStreamManager)
	mock_io.is_running = True
	mock_io.consumer_count = 1
	mock_io.latest_frame = b"frame"

	mock_tool = MagicMock(spec=ToolCameraStreamManager)
	mock_tool.is_running = True
	mock_tool.consumer_count = 0
	mock_tool.latest_frame = None

	with patch.dict(camera_registry, {"io": mock_io, "tool": mock_tool}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 2

	camera_ids = [c["id"] for c in data["cameras"]]
	assert "io" in camera_ids
	assert "tool" in camera_ids


@pytest.mark.anyio
async def test_stream_camera_not_found(client: AsyncClient) -> None:
	with patch.dict(camera_registry, {}, clear=True):
		response = await client.get("/api/camera/nonexistent")

	assert response.status_code == 404
	data = response.json()
	assert "not found" in data["detail"].lower()


@pytest.mark.anyio
async def test_stream_camera_invalid_id(client: AsyncClient) -> None:
	mock_io = MagicMock(spec=CameraStreamManager)

	with patch.dict(camera_registry, {"io": mock_io}, clear=True):
		response = await client.get("/api/camera/invalid_camera")

	assert response.status_code == 404




@pytest.mark.anyio
async def test_camera_types_identified_correctly(client: AsyncClient) -> None:
	io_manager = MagicMock(spec=CameraStreamManager)
	io_manager.is_running = False
	io_manager.consumer_count = 0
	io_manager.latest_frame = None

	tool_manager = MagicMock(spec=ToolCameraStreamManager)
	tool_manager.is_running = False
	tool_manager.consumer_count = 0
	tool_manager.latest_frame = None

	with patch.dict(camera_registry, {"io": io_manager, "tool": tool_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()

	io_camera = next((c for c in data["cameras"] if c["id"] == "io"), None)
	tool_camera = next((c for c in data["cameras"] if c["id"] == "tool"), None)

	assert io_camera is not None
	assert io_camera["type"] == "AFSCam"

	assert tool_camera is not None
	assert tool_camera["type"] == "ToolCamera"
