from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sentry_sdk
import pytest
from fastapi import HTTPException
from openshelf_os_server import main
from openshelf_os_server.routers import report
from openshelf_os_server.settings import clear_config_cache


def _set_mode_file(tmp_path, mode: str):
	mode_file = tmp_path / ".mode"
	mode_file.write_text(mode, encoding="utf-8")
	return mode_file



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
			"message": "No response received after 3 retries, continue with next request",
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


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
		"body": "No response received after 3 retries, continue with next request",
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
