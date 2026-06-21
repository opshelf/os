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


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry() -> None:
	event = {
		"logentry": {
			"message": "ERROR: request ask for id=2 but got id=3, Skipping.",
			"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert main._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_log_drops_pymodbus_request_id_mismatch() -> None:
	log = {
		"body": "ERROR: request ask for id=2 but got id=3, Skipping.",
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
		patch.object(report.sentry_sdk, "push_scope") as push_scope_mock,
		patch.object(report.sentry_sdk, "capture_message") as capture_message_mock,
	):
		clear_config_cache()
		created = await report.submit_error_report(report.ReportErrorRequest(message="boom"), current_user=fake_user)
		assert created.status == "ok"

		push_scope_mock.assert_not_called()
		capture_message_mock.assert_not_called()
