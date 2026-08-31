from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os_server.core.camera import (
	camera_registry,
	init_camera_progress,
	ToolCameraStreamManager,
)
from openshelf_os_server.core.init_progress import OVERALL_READY, RobotInitTracker


def _make_manager_mock(is_delivering_frames: bool) -> MagicMock:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = False
	manager.consumer_count = 0
	manager.background_consumer_count = 0
	manager.first_frame_timeout = 15.0

	async def start() -> None:
		manager.is_running = True

	async def stop() -> None:
		manager.is_running = False

	manager.start = AsyncMock(side_effect=start)
	manager.stop = AsyncMock(side_effect=stop)
	manager.increment_consumers = MagicMock(return_value=1)
	manager.decrement_consumers = MagicMock(return_value=0)
	manager.wait_for_fresh_frame = AsyncMock(return_value=is_delivering_frames)
	return manager


def _components(tracker: RobotInitTracker) -> dict[str, dict]:
	return {c["component"]: c for c in tracker.snapshot()["components"]}


@pytest.mark.anyio
async def test_init_camera_progress_reports_ready_when_frames_arrive() -> None:
	tracker = RobotInitTracker()
	manager = _make_manager_mock(is_delivering_frames=True)

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		with patch("openshelf_os_server.core.camera.get_init_tracker", return_value=tracker):
			await init_camera_progress(["tool"])

	assert _components(tracker)["tool_camera"]["phase"] == "ready"
	assert tracker.failed_components() == []
	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_init_camera_progress_reports_failed_when_no_frames_arrive() -> None:
	tracker = RobotInitTracker()
	manager = _make_manager_mock(is_delivering_frames=False)

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		with patch("openshelf_os_server.core.camera.get_init_tracker", return_value=tracker):
			await init_camera_progress(["tool"])

	component = _components(tracker)["tool_camera"]
	assert component["phase"] == "failed"
	assert component["reason"] == "no frames within 15s"
	assert tracker.failed_components() == ["tool_camera"]
	manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_init_camera_progress_reports_failed_when_camera_is_not_registered() -> None:
	tracker = RobotInitTracker()

	with patch.dict(camera_registry, {}, clear=True):
		with patch("openshelf_os_server.core.camera.get_init_tracker", return_value=tracker):
			await init_camera_progress(["iobox_sizer"])

	component = _components(tracker)["iobox_sizer_camera"]
	assert component["phase"] == "failed"
	assert component["reason"] == "camera not registered"


@pytest.mark.anyio
async def test_init_camera_progress_failure_does_not_degrade_overall_state() -> None:
	tracker = RobotInitTracker()
	manager = _make_manager_mock(is_delivering_frames=False)

	with patch.dict(camera_registry, {"tool": manager}, clear=True):
		with patch("openshelf_os_server.core.camera.get_init_tracker", return_value=tracker):
			tracker.set_overall(OVERALL_READY)
			await init_camera_progress(["tool"])

	assert _components(tracker)["tool_camera"]["phase"] == "failed"
	assert tracker.failed_components() == ["tool_camera"]
	assert tracker.failed_components(degrading_only=True) == []
	assert tracker.snapshot()["phase"] == OVERALL_READY


@pytest.mark.anyio
async def test_init_camera_progress_probes_every_camera() -> None:
	tracker = RobotInitTracker()
	tool_manager = _make_manager_mock(is_delivering_frames=True)
	overhead_manager = _make_manager_mock(is_delivering_frames=False)

	registry = {"tool": tool_manager, "overhead": overhead_manager}
	with patch.dict(camera_registry, registry, clear=True):
		with patch("openshelf_os_server.core.camera.get_init_tracker", return_value=tracker):
			await init_camera_progress(["tool", "overhead"])

	components = _components(tracker)
	assert components["tool_camera"]["phase"] == "ready"
	assert components["overhead_camera"]["phase"] == "failed"
	tool_manager.wait_for_fresh_frame.assert_awaited_once_with(15.0)
	overhead_manager.wait_for_fresh_frame.assert_awaited_once_with(15.0)
