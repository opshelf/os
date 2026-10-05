from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from openshelf_os_server.core.camera import (
	camera_registry,
	CameraStreamManager,
	ToolCameraStreamManager,
	stop_camera_stream_by_id,
)


def test_tool_stream_manager_skips_source_frames_for_target_fps() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	assert manager.target_fps == 15
	cap = MagicMock()
	cap.get.return_value = 60

	frames_to_skip = manager._get_frames_to_skip(cap)

	assert frames_to_skip == 3


def test_raw_camera_uses_lower_fps_for_background_recorder() -> None:
	manager = CameraStreamManager(camera=MagicMock())
	manager.increment_background_consumers()

	assert manager.effective_target_fps() == 3

	manager.increment_consumers()
	assert manager.effective_target_fps() == 12

	manager.decrement_consumers()
	assert manager.effective_target_fps() == 3


@pytest.mark.anyio
async def test_tool_camera_is_offline_when_rtsp_host_is_unreachable() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://127.0.0.1:1/stream")

	assert await manager.is_available() is False


@pytest.mark.anyio
async def test_stop_camera_stream_by_id_stops_manager_when_last_consumer_disconnects() -> None:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.consumer_count = 0
	manager.nalu_consumer_count = 0
	manager.background_consumer_count = 0
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		await stop_camera_stream_by_id("tool")

	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_stop_camera_stream_keeps_manager_running_for_background_recorder() -> None:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.consumer_count = 0
	manager.nalu_consumer_count = 0
	manager.background_consumer_count = 1
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.stop = AsyncMock()

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		await stop_camera_stream_by_id("tool")

	manager.stop.assert_not_awaited()
