from __future__ import annotations

import openshelf_os


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
			"message": "No response received after 3 retries, continue with next request",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


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


def test_before_send_sentry_event_drops_door_motor_recovery_failure_logentry() -> None:
	event = {
		"logentry": {
			"message": "Door motor recovery failed: %s",
			"formatted": (
				"Door motor recovery failed: Failed during pre-home approach: "
				"Movement failed for motor 31 (status=FAULT)"
			),
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_door_motor_recovery_failure_entry() -> None:
	event = {
		"entries": [
			{
				"data": {
					"formatted": (
						"Door motor recovery failed: Failed during pre-home approach: "
						"Movement failed for motor 31 (status=FAULT)"
					),
				}
			}
		]
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_unrelated_door_recovery_message() -> None:
	event = {
		"logentry": {
			"message": "Door position recovered before homing; skipping recovery home",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) == event


def test_before_send_sentry_log_drops_door_motor_recovery_failure() -> None:
	log = {
		"body": (
			"Door motor recovery failed: Failed during pre-home approach: "
			"Movement failed for motor 31 (status=FAULT)"
		),
		"attributes": {"motor_id": 31},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_door_motor_recovery_failure_template_attribute() -> None:
	log = {
		"body": "",
		"attributes": {"sentry.message.template": "Door motor recovery failed: %s"},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_message() -> None:
	log = {
		"body": "No response received after 3 retries, continue with next request",
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_pymodbus_no_response_retry_attribute() -> None:
	log = {
		"body": "",
		"attributes": {"message": "No response received after 12 retries, continue with next request"},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None


def test_before_send_sentry_log_drops_door_motor_recovery_message() -> None:
	log = {
		"body": openshelf_os.DOOR_MOTOR_RECOVERY_EVENT,
		"attributes": {"bad_reading_count": 5},
	}

	assert openshelf_os._before_send_sentry_log(log, {}) is None
