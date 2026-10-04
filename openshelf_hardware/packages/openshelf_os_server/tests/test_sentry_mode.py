from __future__ import annotations

import os
from unittest.mock import patch

import sentry_sdk
import pytest
from openshelf_os_server import main
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


def test_before_send_sentry_event_drops_sse_cancel_on_finish_cancelled_error() -> None:
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


# The value anyio puts on a CancelledError it delivered on behalf of BaseHTTPMiddleware's
# call_next task, which is how a streaming request is cancelled when its response finishes.
HTTP_MIDDLEWARE_CANCEL = (
	"Cancelled via cancel scope 7fff50ee13d0 by "
	"<Task cancelling name='starlette.middleware.base.BaseHTTPMiddleware.__call__."
	"<locals>.call_next.<locals>.coro' coro=<BaseHTTPMiddleware.__call__."
	"<locals>.call_next.<locals>.coro() running at /app/openshelf_hardware/.venv/"
	"lib/python3.13/site-packages/starlette/middleware/base.py:144> "
	"cb=[TaskGroup._spawn.<locals>.task_done() at /app/openshelf_hardware/.venv/"
	"lib/python3.13/site-packages/anyio/_backends/_asyncio.py:803]>"
)


def test_before_send_sentry_event_keeps_error_chained_to_sse_cancel() -> None:
	# The same guard applies to the pre-existing sse-starlette canceller: a real failure chained to
	# its cancellation stays reportable.
	event = {
		"exception": {
			"values": [
				{"type": "ConnectionResetError", "value": "tool camera frame queue lost"},
				{
					"type": "CancelledError",
					"value": (
						"Cancelled via cancel scope 7ffea42fa710 by "
						"<Task pending name='sse_starlette.sse.EventSourceResponse.__call__."
						"<locals>.cancel_on_finish'>"
					),
				},
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_keeps_task_group_error_under_http_middleware_cancel() -> None:
	# anyio reports a task group failure as a group next to the cancellation it also caused, and a
	# group is not one of the stream types the filter tolerates.
	event = {
		"exception": {
			"values": [
				{
					"type": "ExceptionGroup",
					"value": "unhandled errors in a TaskGroup (1 sub-exception)",
				},
				{"type": "CancelledError", "value": HTTP_MIDDLEWARE_CANCEL},
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_keeps_error_under_uvicorn_shutdown_cancel() -> None:
	# A failure that was already in flight under the shutdown cancellation stays reportable.
	event = {
		"exception": {
			"values": [
				{"type": "ValueError", "value": "bad motor position"},
				{"type": "CancelledError", "value": main.UVICORN_SHUTDOWN_CANCEL_MESSAGE},
			]
		}
	}

	assert main._before_send_sentry_event(event, {}) == event


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

