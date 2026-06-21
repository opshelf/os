from __future__ import annotations

import openshelf_os


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry() -> None:
	event = {
		"logentry": {
			"message": "ERROR: request ask for id=2 but got id=3, Skipping.",
			"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None
