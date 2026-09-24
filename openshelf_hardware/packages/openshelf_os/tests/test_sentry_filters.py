from __future__ import annotations

import openshelf_os

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


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry_message() -> None:
	event = {
		"logentry": {
			"message": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry_formatted() -> None:
	event = {
		"logentry": {
			"message": "unformatted message",
			"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_logentry() -> None:
	event = {
		"logentry": {
			"message": PYMODBUS_RETRY_TEXT,
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_with_frame_dump() -> None:
	event = {
		"logentry": {
			"message": PYMODBUS_FRAME_DUMP,
			"formatted": PYMODBUS_FRAME_DUMP,
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_logentry_formatted_with_frame_dump() -> None:
	event = {
		"logentry": {
			"message": "unformatted message",
			"formatted": PYMODBUS_FRAME_DUMP,
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_with_repeating_frame() -> None:
	event = {"message": PYMODBUS_REPEATING_FRAME_DUMP}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_pymodbus_no_response_retry_with_other_trailer() -> None:
	event = {
		"logentry": {
			"formatted": PYMODBUS_RETRY_TEXT + "\nTraceback (most recent call last):",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_keeps_pymodbus_no_response_disconnect() -> None:
	event = {
		"logentry": {
			"message": "ERROR: No response received of the last requests (default: retries+3), CLOSING CONNECTION.",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_event_drops_door_motor_recovery_message() -> None:
	event = {
		"logentry": {
			"message": openshelf_os.DOOR_MOTOR_RECOVERY_EVENT,
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_message() -> None:
	log = {
		"body": PYMODBUS_RETRY_TEXT,
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_attribute() -> None:
	log = {
		"body": "",
		"attributes": {"message": "No response received after 12 retries, continue with next request"},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_with_frame_dump() -> None:
	log = {"body": PYMODBUS_FRAME_DUMP}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_with_repeating_frame() -> None:
	log = {"body": PYMODBUS_REPEATING_FRAME_DUMP}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_keeps_pymodbus_no_response_retry_with_other_trailer() -> None:
	log = {"body": PYMODBUS_RETRY_TEXT + "\nTraceback (most recent call last):"}

	assert openshelf_os._before_send_sentry_log(log, {}) == log


def test_before_send_sentry_log_drops_door_motor_recovery_message() -> None:
	log = {
		"body": openshelf_os.DOOR_MOTOR_RECOVERY_EVENT,
		"attributes": {"bad_reading_count": 5},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None
