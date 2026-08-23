import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import numpy as np
import pytest
from httpx import AsyncClient

from openshelf_os_server.core.camera import (
	camera_registry,
	CameraStreamManager,
	ToolCameraStreamManager,
	init_overview_camera_manager,
	is_rtsp_url_reachable,
	stop_camera_stream_by_id,
)

from openshelf_os_server.main import _get_overview_camera_urls, _get_tool_camera_url


def _make_sizer_manager_mock(is_available: bool = True) -> MagicMock:
	manager = MagicMock(spec=CameraStreamManager)
	manager.is_running = True
	manager.consumer_count = 2
	manager.latest_frame = b"fake_frame"
	manager.camera_type = "IOBoxSizerCamera"
	manager.is_available = AsyncMock(return_value=is_available)
	return manager


def _make_tool_manager_mock(is_available: bool = True) -> MagicMock:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = False
	manager.consumer_count = 0
	manager.latest_frame = None
	manager.is_available = AsyncMock(return_value=is_available)
	return manager


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
	mock_manager = _make_sizer_manager_mock()

	with patch.dict(camera_registry, {"iobox_sizer": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 1

	camera = data["cameras"][0]
	assert camera["id"] == "iobox_sizer"
	assert camera["type"] == "IOBoxSizerCamera"
	assert camera["online"] is True
	assert camera["running"] is True
	assert camera["consumers"] == 2
	assert camera["has_frame"] is True


@pytest.mark.anyio
async def test_list_cameras_with_tool_camera(client: AsyncClient) -> None:
	mock_manager = _make_tool_manager_mock()

	with patch.dict(camera_registry, {"tool": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	data = response.json()
	assert len(data["cameras"]) == 1

	camera = data["cameras"][0]
	assert camera["id"] == "tool"
	assert camera["type"] == "ToolCamera"
	assert camera["online"] is True
	assert camera["running"] is False
	assert camera["consumers"] == 0
	assert camera["has_frame"] is False


@pytest.mark.anyio
async def test_list_cameras_with_overview_cameras(client: AsyncClient) -> None:
	one_manager = _make_tool_manager_mock()
	two_manager = _make_tool_manager_mock()

	with patch.dict(
		camera_registry,
		{"overview_one": one_manager, "overview_two": two_manager},
		clear=True,
	):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	assert [(camera["id"], camera["type"]) for camera in response.json()["cameras"]] == [
		("overview_one", "OverviewCamera"),
		("overview_two", "OverviewCamera"),
	]


@pytest.mark.anyio
async def test_list_cameras_reports_unreachable_tool_camera_as_offline(client: AsyncClient) -> None:
	mock_manager = _make_tool_manager_mock(is_available=False)

	with patch.dict(camera_registry, {"tool": mock_manager}, clear=True):
		response = await client.get("/api/camera")

	assert response.status_code == 200
	camera = response.json()["cameras"][0]
	assert camera["id"] == "tool"
	assert camera["online"] is False


@pytest.mark.anyio
async def test_list_cameras_multiple(client: AsyncClient) -> None:
	mock_sizer = _make_sizer_manager_mock()
	mock_tool = _make_tool_manager_mock()

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
async def test_stream_camera_unavailable_when_no_frames_arrive(client: AsyncClient) -> None:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.consumer_count = 0
	manager.latest_frame = None
	manager.first_frame_timeout = 0.2
	manager.start = AsyncMock()
	manager.stop = AsyncMock()
	manager.increment_consumers = MagicMock(return_value=1)
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.wait_for_fresh_frame = AsyncMock(return_value=False)

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		response = await client.get("/api/camera/tool")

	assert response.status_code == 503
	assert "not delivering frames" in response.json()["detail"]
	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_camera_types_identified_correctly(client: AsyncClient) -> None:
	sizer_manager = _make_sizer_manager_mock()
	sizer_manager.is_running = False
	sizer_manager.consumer_count = 0
	sizer_manager.latest_frame = None

	tool_manager = _make_tool_manager_mock()

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
	monkeypatch.setattr("openshelf_os_server.main.CONFIG_PATH", data_path / "config.json")

	settings = SimpleNamespace(tool_camera=SimpleNamespace(enabled=True, rtsp_url=""))

	assert _get_tool_camera_url(settings) == "rtsp://camera"


def test_get_overview_camera_urls_returns_only_enabled_configured_cameras() -> None:
	settings = SimpleNamespace(
		overview_one_camera=SimpleNamespace(enabled=True, rtsp_url="rtsp://one"),
		overview_two_camera=SimpleNamespace(enabled=False, rtsp_url="rtsp://two"),
	)

	assert _get_overview_camera_urls(settings) == {"overview_one": "rtsp://one"}


@pytest.mark.anyio
async def test_init_overview_camera_manager_registers_named_camera() -> None:
	with patch.dict(camera_registry, {}, clear=True):
		await init_overview_camera_manager("overview_one", "rtsp://one")

		manager = camera_registry["overview_one"]
		assert isinstance(manager, ToolCameraStreamManager)
		assert manager.rtsp_url == "rtsp://one"


@pytest.mark.anyio
async def test_init_overview_camera_manager_rejects_unknown_id() -> None:
	with pytest.raises(ValueError, match="Unsupported overview camera id"):
		await init_overview_camera_manager("overview_center", "rtsp://center")

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
async def test_stream_manager_frame_is_stale_until_a_frame_is_captured() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")

	assert manager.is_frame_stale() is True

	manager.last_frame_at = time.monotonic()

	assert manager.is_frame_stale() is False


@pytest.mark.anyio
async def test_stream_manager_frame_is_stale_after_the_timeout_elapses() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.last_frame_at = time.monotonic() - (manager.stale_frame_timeout + 1.0)

	assert manager.is_frame_stale() is True


@pytest.mark.anyio
async def test_wait_for_fresh_frame_times_out_when_no_frames_arrive() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")

	assert await manager.wait_for_fresh_frame(0.2) is False


@pytest.mark.anyio
async def test_wait_for_fresh_frame_returns_true_once_a_frame_arrives() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.last_frame_at = time.monotonic()

	assert await manager.wait_for_fresh_frame(0.2) is True


@pytest.mark.anyio
async def test_is_rtsp_url_reachable_is_false_for_an_unreachable_host() -> None:
	assert await is_rtsp_url_reachable("rtsp://127.0.0.1:1/stream", timeout=0.5) is False


@pytest.mark.anyio
async def test_is_rtsp_url_reachable_is_true_when_the_port_accepts_connections() -> None:
	server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
	port = server.sockets[0].getsockname()[1]

	async with server:
		assert await is_rtsp_url_reachable(f"rtsp://127.0.0.1:{port}/stream", timeout=1.0) is True


@pytest.mark.anyio
async def test_tool_camera_is_offline_when_rtsp_host_is_unreachable() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://127.0.0.1:1/stream")

	assert await manager.is_available() is False


@pytest.mark.anyio
async def test_tool_camera_is_online_while_delivering_fresh_frames() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://127.0.0.1:1/stream")
	manager.is_running = True
	manager.last_frame_at = time.monotonic()

	assert await manager.is_available() is True


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
