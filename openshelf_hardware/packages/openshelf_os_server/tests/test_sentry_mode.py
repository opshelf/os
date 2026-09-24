from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sentry_sdk
import pytest
from fastapi import HTTPException
from openshelf_os_server import main
from openshelf_os_server.core.camera import CameraStreamManager, ToolCameraStreamManager
from openshelf_os_server.routers import report
from openshelf_os_server.settings import clear_config_cache


def _set_mode_file(tmp_path, mode: str):
	mode_file = tmp_path / ".mode"
	mode_file.write_text(mode, encoding="utf-8")
	return mode_file


# pymodbus' Log.error appends the last frames when DEBUG logging is off, so the
# logged message is the retry text plus that trailer.
PYMODBUS_RETRY_TEXT = "No response received after 3 retries, continue with next request"
PYMODBUS_FRAME_DUMP = (
	PYMODBUS_RETRY_TEXT + "\n"
	">>>>> recv: 0x5 0x6 0x0 0x7 0x0 0x1 0xf8 0x4f extra data: \n"
	">>>>> send: 0x5 0x6 0x62 0x3 0x0 0xc8 0x66 0x60"
)
# pymodbus collapses a repeated frame in the dump to this marker.
PYMODBUS_REPEATING_FRAME_DUMP = (
	PYMODBUS_RETRY_TEXT + "\n"
	">>>>> send: 0x1f 0x3 0x60 0x2c 0x0 0x2 0x18 0x7c\n"
	">>>>> Repeating....\n"
	">>>>> send: 0x1f 0x3 0x60 0x2c 0x0 0x2 0x18 0x7c"
)



@pytest.mark.anyio
async def test_configure_sentry_skips_development(tmp_path) -> None:
	mode_file = _set_mode_file(tmp_path, "development")

	with (
		patch("openshelf_os_server.settings.MODE_FILE", mode_file),
		patch.dict(os.environ, {"SENTRY_DSN_API": "https://example.invalid/1"}, clear=False),
		patch.object(sentry_sdk, "init") as init_mock,
	):
		clear_config_cache()
		assert main._configure_sentry() is False
		init_mock.assert_not_called()


@pytest.mark.anyio
async def test_configure_sentry_skips_mock(tmp_path) -> None:
	mode_file = _set_mode_file(tmp_path, "mock")

	with (
		patch("openshelf_os_server.settings.MODE_FILE", mode_file),
		patch.dict(os.environ, {"SENTRY_DSN_API": "https://example.invalid/1"}, clear=False),
		patch.object(sentry_sdk, "init") as init_mock,
	):
		clear_config_cache()
		assert main._configure_sentry() is False
		init_mock.assert_not_called()



@pytest.mark.anyio
async def test_configure_sentry_skips_when_dsn_missing(tmp_path) -> None:
	for mode in ("demo", "production"):
		mode_file = _set_mode_file(tmp_path, mode)

		with (
			patch("openshelf_os_server.settings.MODE_FILE", mode_file),
			patch.dict(os.environ, {}, clear=False),
			patch.object(sentry_sdk, "init") as init_mock,
		):
			os.environ.pop("SENTRY_DSN_API", None)
			clear_config_cache()
			assert main._configure_sentry() is False
			init_mock.assert_not_called()



@pytest.mark.anyio
async def test_configure_sentry_enables_demo_and_production(tmp_path) -> None:
	for mode in ("demo", "production"):
		mode_file = _set_mode_file(tmp_path, mode)

		with (
			patch("openshelf_os_server.settings.MODE_FILE", mode_file),
			patch.dict(os.environ, {"SENTRY_DSN_API": "https://example.invalid/1"}, clear=False),
			patch.object(sentry_sdk, "init") as init_mock,
			patch.object(sentry_sdk, "set_tag") as set_tag_mock,
			patch.object(sentry_sdk, "set_context") as set_context_mock,
		):
			clear_config_cache()
			assert main._configure_sentry() is True
			init_mock.assert_called_once()
			set_tag_mock.assert_called()
			set_context_mock.assert_called()


def test_before_send_sentry_event_drops_anyio_cancelled_error() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "CancelledError",
					"value": (
						"Cancelled via cancel scope 7ffea42fa710 by "
						"<Task pending name='sse_starlette.sse.EventSourceResponse.__call__."
						"<locals>.cancel_on_finish'>"
					),
				}
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_plain_cancelled_error() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "CancelledError",
					"value": "Worker task was cancelled unexpectedly",
				}
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch() -> None:
	event = {
		"message": "ERROR: request ask for id=2 but got id=3, Skipping.",
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_entry() -> None:
	event = {
		"entries": [
			{
				"data": {
					"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
				}
			}
		]
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry_message() -> None:
	event = {
		"logentry": {
			"message": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry_formatted() -> None:
	event = {
		"logentry": {
			"message": "unformatted message",
			"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_logentry() -> None:
	event = {
		"logentry": {
			"message": PYMODBUS_RETRY_TEXT,
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_with_frame_dump() -> None:
	event = {
		"logentry": {
			"message": PYMODBUS_FRAME_DUMP,
			"formatted": PYMODBUS_FRAME_DUMP,
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_logentry_formatted_with_frame_dump() -> None:
	event = {
		"logentry": {
			"message": "unformatted message",
			"formatted": PYMODBUS_FRAME_DUMP,
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_with_repeating_frame() -> None:
	event = {"message": PYMODBUS_REPEATING_FRAME_DUMP}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_pymodbus_no_response_retry_with_other_trailer() -> None:
	event = {
		"logentry": {
			"formatted": PYMODBUS_RETRY_TEXT + "\nTraceback (most recent call last):",
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_keeps_pymodbus_no_response_disconnect() -> None:
	event = {
		"logentry": {
			"message": "ERROR: No response received of the last requests (default: retries+3), CLOSING CONNECTION.",
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_drops_http_exception_from_hint() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "HTTPException",
					"value": "{'message': 'Motor move failed', 'code': 'motor_move_failed'}",
				}
			]
		}
	}
	hint = {"original_exception": HTTPException(status_code=503, detail="Motor move failed")}

	assert main._before_send_sentry_event(event, hint) is None


def test_before_send_sentry_event_drops_http_exception_from_event() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "fastapi.exceptions.HTTPException",
					"value": "{'message': 'Motor move failed', 'code': 'motor_move_failed'}",
				}
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_door_motor_recovery_message() -> None:
	event = {
		"logentry": {
			"message": main.DOOR_MOTOR_RECOVERY_EVENT,
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_log_drops_pymodbus_request_id_mismatch() -> None:
	log = {
		"body": "ERROR: request ask for id=2 but got id=3, Skipping.",
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry() -> None:
	log = {
		"body": PYMODBUS_RETRY_TEXT,
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_attribute() -> None:
	log = {
		"body": "",
		"attributes": {
			"message": "No response received after 12 retries, continue with next request",
		},
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_with_frame_dump() -> None:
	log = {"body": PYMODBUS_FRAME_DUMP}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_with_repeating_frame() -> None:
	log = {"body": PYMODBUS_REPEATING_FRAME_DUMP}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_keeps_pymodbus_no_response_retry_with_other_trailer() -> None:
	log = {"body": PYMODBUS_RETRY_TEXT + "\nTraceback (most recent call last):"}

	assert main._before_send_sentry_log(log, {}) == log


def test_before_send_sentry_log_drops_robot_initialization_failed_log() -> None:
	log = {
		"body": "robot_initialization_failed",
		"attributes": {
			"error": "Motor bus connection failed (/dev/ttyUSB0: [Errno 2] No such file or directory)",
			"error_type": "ConfigError",
		},
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_robot_initialization_failed_attribute() -> None:
	log = {
		"body": "",
		"attributes": {
			"event": "robot_initialization_failed",
			"error_type": "ConfigError",
		},
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_tool_fault_log() -> None:
	log = {
		"body": "Tool fault detected - manual intervention required",
		"attributes": {
			"exception_type": "MovementError",
			"exception_message": "Gantry movement failed: x=FAULT, y=STOPPED",
		},
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_tool_fault_attribute() -> None:
	log = {
		"body": "",
		"attributes": {
			"message": "Tool fault detected - manual intervention required",
			"exception_type": "MovementError",
		},
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_door_motor_recovery_log() -> None:
	log = {
		"body": main.DOOR_MOTOR_RECOVERY_EVENT,
	}

	assert main._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_event_redacts_headers() -> None:
	event = {
		"request": {
			"headers": {
				"authorization": "secret",
				"x-request-id": "request-id",
			}
		}
	}

	assert main._before_send_sentry_event(event, {}) == event
	assert event["request"]["headers"]["authorization"] == "[Filtered]"
	assert event["request"]["headers"]["x-request-id"] == "request-id"


def test_before_send_sentry_event_drops_robot_unavailable_http_exception() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "HTTPException",
					"value": "503: Robot is not initialized or failed to start",
				}
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_robot_worker_not_initialized_request() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "Robot Worker was Not Initialized",
				}
			]
		},
		"request": {"url": "http://device.local/api/robot/debug/motors"},
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_calibration_robot_worker_not_initialized_request() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "Robot Worker was Not Initialized",
				}
			]
		},
		"request": {"url": "http://device.local/api/calibration/status"},
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_calibration_robot_not_initialized_request() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "503: Robot is not initialized",
				}
			]
		},
		"request": {"url": "http://device.local/api/calibration/status"},
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_robot_worker_not_initialized_for_unmatched_path() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "Robot Worker was Not Initialized",
				}
			]
		},
		"request": {"url": "http://device.local/api/robotics/status"},
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_drops_robot_unavailable_exception_chain() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "Robot Worker was Not Initialized",
				},
				{
					"type": "fastapi.exceptions.HTTPException",
					"value": "503: Robot is not initialized or failed to start",
				},
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_robot_worker_not_initialized_without_robot_request() -> None:
	event = {
		"exception": {
			"values": [
				{
					"type": "ValueError",
					"value": "Robot Worker was Not Initialized",
				}
			]
		},
		"request": {"url": "http://device.local/api/health"},
	}

	assert main._before_send_sentry_event(event, {}) == event


@pytest.mark.anyio
async def test_report_error_skips_sentry_in_development(tmp_path) -> None:
	mode_file = _set_mode_file(tmp_path, "development")
	system_context = {
		"device_id": "dev",
		"hostname": "host",
		"environment": "development",
		"schema_version": 4,
		"version": "1",
	}
	fake_user = SimpleNamespace(
		id=1,
		email="admin@opshelf.com",
		first_name="Admin",
		last_name="User",
		role=SimpleNamespace(name="admin"),
	)

	with (
		patch("openshelf_os_server.settings.MODE_FILE", mode_file),
		patch.dict(
			os.environ,
			{"SENTRY_DSN_API": "https://example.invalid/1", "SENTRY_ENVIRONMENT": "production"},
			clear=False,
		),
		patch.object(report, "_build_robot_context", new=AsyncMock(return_value={"status": "ok"})),
		patch.object(report, "_build_system_context", return_value=system_context),
		patch.object(report, "_capture_camera_images", new=AsyncMock(return_value={})),
		patch.object(report.sentry_sdk, "new_scope") as new_scope_mock,
		patch.object(report.sentry_sdk, "capture_message") as capture_message_mock,
	):
		clear_config_cache()
		created = await report.submit_error_report(report.ReportErrorRequest(message="boom"), current_user=fake_user)
		assert created.status == "ok"

		new_scope_mock.assert_not_called()
		capture_message_mock.assert_not_called()


@pytest.mark.anyio
async def test_capture_camera_images_leases_idle_rtsp_camera() -> None:
	# With no /camera consumer an RTSP manager runs on a background (crash-
	# recorder) lease only: it is running, but consumer_count == 0 and the
	# MJPEG decode worker (latest_frame) is stopped.
	tool_manager = MagicMock(spec=ToolCameraStreamManager)
	tool_manager.is_running = True
	tool_manager.consumer_count = 0
	tool_manager.latest_frame = None
	tool_manager.first_frame_timeout = 15.0
	tool_manager.wait_for_fresh_frame = AsyncMock(return_value=True)

	dead_tool_manager = MagicMock(spec=ToolCameraStreamManager)
	dead_tool_manager.is_running = True
	dead_tool_manager.consumer_count = 0
	dead_tool_manager.latest_frame = None
	dead_tool_manager.first_frame_timeout = 15.0
	dead_tool_manager.wait_for_fresh_frame = AsyncMock(return_value=False)

	# Previously viewed RTSP camera whose last viewer left: the MJPEG decode
	# worker stopped, but an old latest_frame is still in memory (stale path).
	stale_tool_manager = MagicMock(spec=ToolCameraStreamManager)
	stale_tool_manager.is_running = True
	stale_tool_manager.consumer_count = 0
	stale_tool_manager.latest_frame = b"stale_frame"
	stale_tool_manager.first_frame_timeout = 15.0
	stale_tool_manager.wait_for_fresh_frame = AsyncMock(return_value=True)
	stale_tool_manager.mjpeg_path_stale = MagicMock(return_value=True)

	# Previously viewed RTSP camera with a fresh MJPEG path (a viewer is
	# open): attach its frame without taking a lease.
	fresh_tool_manager = MagicMock(spec=ToolCameraStreamManager)
	fresh_tool_manager.is_running = True
	fresh_tool_manager.consumer_count = 0
	fresh_tool_manager.latest_frame = b"fresh_frame"
	fresh_tool_manager.first_frame_timeout = 15.0
	fresh_tool_manager.wait_for_fresh_frame = AsyncMock(return_value=True)
	fresh_tool_manager.mjpeg_path_stale = MagicMock(return_value=False)

	sizer_manager = MagicMock(spec=CameraStreamManager)
	sizer_manager.is_running = True
	sizer_manager.latest_frame = b"sizer_frame"
	sizer_manager.get_latest_frame = AsyncMock(return_value=b"sizer_frame")

	frames: dict[str, bytes] = {"tool_stale": b"stale_frame", "tool_fresh": b"fresh_frame"}

	def make_get_latest_frame(camera_id: str):
		async def get_latest_frame() -> bytes | None:
			return frames.get(camera_id)

		return get_latest_frame

	tool_manager.get_latest_frame = AsyncMock(side_effect=make_get_latest_frame("tool"))
	dead_tool_manager.get_latest_frame = AsyncMock(side_effect=make_get_latest_frame("tool_dead"))
	stale_tool_manager.get_latest_frame = AsyncMock(side_effect=make_get_latest_frame("tool_stale"))
	fresh_tool_manager.get_latest_frame = AsyncMock(side_effect=make_get_latest_frame("tool_fresh"))

	async def fake_start_camera_stream(camera_id: str) -> None:
		frames[camera_id] = b"frame"

	start_camera_stream_mock = AsyncMock(side_effect=fake_start_camera_stream)
	stop_camera_stream_mock = AsyncMock()

	with (
		patch.dict(
			report.camera_registry,
			{
				"tool": tool_manager,
				"tool_dead": dead_tool_manager,
				"tool_stale": stale_tool_manager,
				"tool_fresh": fresh_tool_manager,
				"iobox_sizer": sizer_manager,
			},
			clear=True,
		),
		patch.object(report, "start_camera_stream_by_id", start_camera_stream_mock),
		patch.object(report, "stop_camera_stream_by_id", stop_camera_stream_mock),
	):
		images = await report._capture_camera_images()

	# The idle and stale RTSP cameras each got a temporary MJPEG consumer
	# lease and produced a fresh snapshot (the stale one replaced the hour-old
	# in-memory frame); the fresh RTSP camera and the UVC camera kept their
	# existing behavior with no lease.
	assert images == {
		"tool": b"frame",
		"tool_stale": b"frame",
		"tool_fresh": b"fresh_frame",
		"iobox_sizer": b"sizer_frame",
	}
	assert start_camera_stream_mock.await_count == 3
	start_camera_stream_mock.assert_any_await("tool")
	start_camera_stream_mock.assert_any_await("tool_dead")
	start_camera_stream_mock.assert_any_await("tool_stale")
	leased_ids = [call.args[0] for call in start_camera_stream_mock.await_args_list]
	assert "tool_fresh" not in leased_ids
	assert "iobox_sizer" not in leased_ids
	assert stop_camera_stream_mock.await_count == 3
	tool_manager.wait_for_fresh_frame.assert_awaited_once_with(15.0)
	stale_tool_manager.wait_for_fresh_frame.assert_awaited_once_with(15.0)
	# The stale camera was re-read after the fresh frame arrived, not served
	# the stale in-memory bytes.
	assert stale_tool_manager.get_latest_frame.await_count == 2
	fresh_tool_manager.wait_for_fresh_frame.assert_not_awaited()
	fresh_tool_manager.get_latest_frame.assert_awaited_once()
	sizer_manager.get_latest_frame.assert_awaited_once()
