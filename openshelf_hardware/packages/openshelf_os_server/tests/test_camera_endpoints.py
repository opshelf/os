import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import numpy as np
import pytest
from httpx import AsyncClient

from openshelf_os_server.core.camera import (
	camera_registry,
	CameraStreamManager,
	ToolCameraStreamManager,
	stop_camera_stream_by_id,
)

from openshelf_os_server.main import _get_tool_camera_url

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
	mock_sizer = MagicMock(spec=CameraStreamManager)
	mock_sizer.is_running = True
	mock_sizer.consumer_count = 1
	mock_sizer.latest_frame = b"frame"
	mock_sizer.camera_type = "IOBoxSizerCamera"

	mock_tool = MagicMock(spec=ToolCameraStreamManager)
	mock_tool.is_running = True
	mock_tool.consumer_count = 0
	mock_tool.latest_frame = None

	with patch.dict(camera_registry, {"iobox_sizer": mock_sizer, "tool": mock_tool}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 2

	camera_ids = [c["id"] for c in data["cameras"]]
	assert "iobox_sizer" in camera_ids
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

	tool_manager = MagicMock(spec=ToolCameraStreamManager)
	tool_manager.is_running = False
	tool_manager.consumer_count = 0
	tool_manager.latest_frame = None

	with patch.dict(camera_registry, {"iobox_sizer": sizer_manager, "tool": tool_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()

	sizer_camera = next((c for c in data["cameras"] if c["id"] == "iobox_sizer"), None)
	tool_camera = next((c for c in data["cameras"] if c["id"] == "tool"), None)

	assert sizer_camera is not None
	assert sizer_camera["type"] == "IOBoxSizerCamera"

	assert tool_camera is not None
	assert tool_camera["type"] == "ToolCamera"


def test_get_tool_camera_url_ignores_config_url_when_disabled(tmp_path, monkeypatch) -> None:
	data_path = tmp_path / "data"
	data_path.mkdir()
	(data_path / "config.json").write_text(
		'{"robot":{"tool":{"poe_camera":{"url":"rtsp://camera"}}}}',
		encoding="utf-8",
	)
	monkeypatch.chdir(tmp_path)

	settings = SimpleNamespace(tool_camera=SimpleNamespace(enabled=False, rtsp_url=""))

	assert _get_tool_camera_url(settings) is None


def test_get_tool_camera_url_uses_config_url_when_enabled(tmp_path, monkeypatch) -> None:
	data_path = tmp_path / "data"
	data_path.mkdir()
	(data_path / "config.json").write_text(
		'{"robot":{"tool":{"poe_camera":{"url":"rtsp://camera"}}}}',
		encoding="utf-8",
	)
	monkeypatch.chdir(tmp_path)

	settings = SimpleNamespace(tool_camera=SimpleNamespace(enabled=True, rtsp_url=""))

	assert _get_tool_camera_url(settings) == "rtsp://camera"

@pytest.mark.anyio
async def test_opencv_stream_manager_always_calls_capture_raw() -> None:
	mock_cam = MagicMock()
	mock_cam.capture_raw.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
	manager = CameraStreamManager(camera=mock_cam)
	await manager.start()
	await asyncio.sleep(0.2)
	await manager.stop()
	mock_cam.capture_raw.assert_called()


def test_tool_stream_manager_resizes_wide_frames() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

	resized = manager._prepare_frame(frame)

	assert resized.shape == (360, 640, 3)


def test_tool_stream_manager_keeps_small_frames_unchanged() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	frame = np.zeros((480, 640, 3), dtype=np.uint8)

	resized = manager._prepare_frame(frame)

	assert resized is frame


def test_tool_stream_manager_skips_source_frames_for_target_fps() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	cap = MagicMock()
	cap.get.return_value = 30

	frames_to_skip = manager._get_frames_to_skip(cap)

	assert frames_to_skip == 5


@pytest.mark.anyio
async def test_stop_camera_stream_by_id_stops_manager_when_last_consumer_disconnects() -> None:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		await stop_camera_stream_by_id("tool")

	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_stop_camera_stream_by_id_keeps_manager_running_with_remaining_consumers() -> None:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.decrement_consumers = MagicMock(return_value=1)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		await stop_camera_stream_by_id("tool")

	manager.stop.assert_not_awaited()
