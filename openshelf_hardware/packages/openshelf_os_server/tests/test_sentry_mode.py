from __future__ import annotations

from unittest.mock import Mock

import openshelf_os
import sentry_sdk

from openshelf_os.operating_mode import OperatingMode
from openshelf_os_server import main


def test_demo_mode_disables_sentry(monkeypatch) -> None:
	init = Mock()
	monkeypatch.setattr(sentry_sdk, "init", init)
	monkeypatch.setattr(main, "get_operating_mode", lambda: OperatingMode.demo)
	monkeypatch.setattr(main, "is_service_process", lambda: True)
	monkeypatch.setattr(openshelf_os, "get_operating_mode", lambda: OperatingMode.demo)
	monkeypatch.setattr(openshelf_os, "is_service_process", lambda: True)
	monkeypatch.setenv("SENTRY_DSN_API", "https://public@example.com/1")
	monkeypatch.setattr(openshelf_os, "sentry_dsn", "https://public@example.com/1")

	assert main._configure_sentry() is False
	assert openshelf_os._configure_sentry() is False
	init.assert_not_called()


# The value anyio puts on a CancelledError it delivered on behalf of BaseHTTPMiddleware's
# call_next task, which is how a streaming request is cancelled when its response finishes.


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
