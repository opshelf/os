from __future__ import annotations


from openshelf_os_server import main


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
