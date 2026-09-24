from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
	"logentry",
	[
		pytest.param(
			{"message": "ERROR: request ask for id=2 but got id=3, Skipping."},
			id="message",
		),
		pytest.param(
			{
				"message": "unformatted message",
				"formatted": "ERROR: request ask for id=2 but got id=3, Skipping.",
			},
			id="formatted",
		),
	],
)
def test_before_send_sentry_event_drops_pymodbus_request_id_mismatch_logentry(logentry: dict) -> None:
	event = {
		"logentry": logentry,
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_drops_pymodbus_no_response_retry_logentry() -> None:
	event = {
		"logentry": {
			"message": PYMODBUS_RETRY_TEXT,
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) is None


@pytest.mark.parametrize(
	"logentry",
	[
		pytest.param(
			{"message": PYMODBUS_FRAME_DUMP, "formatted": PYMODBUS_FRAME_DUMP},
			id="message_and_formatted",
		),
		pytest.param(
			{"message": "unformatted message", "formatted": PYMODBUS_FRAME_DUMP},
			id="formatted",
		),
	],
)
def test_before_send_sentry_event_drops_pymodbus_no_response_retry_with_frame_dump(logentry: dict) -> None:
	event = {
		"logentry": logentry,
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


@pytest.mark.parametrize(
	"event",
	[
		pytest.param(
			{
				"logentry": {
					"message": "Door motor recovery failed: %s",
					"formatted": (
						"Door motor recovery failed: Failed during pre-home approach: "
						"Movement failed for motor 31 (status=FAULT)"
					),
				}
			},
			id="logentry",
		),
		pytest.param(
			{
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
			},
			id="entries",
		),
	],
)
def test_before_send_sentry_event_drops_door_motor_recovery_failure(event: dict) -> None:
	assert openshelf_os._before_send_sentry_event(event, {}) is None


def test_before_send_sentry_event_keeps_unrelated_door_recovery_message() -> None:
	event = {
		"logentry": {
			"message": "Door position recovered before homing; skipping recovery home",
		}
	}

	assert openshelf_os._before_send_sentry_event(event, {}) == event


@pytest.mark.parametrize(
	"log",
	[
		pytest.param(
			{
				"body": (
					"Door motor recovery failed: Failed during pre-home approach: "
					"Movement failed for motor 31 (status=FAULT)"
				),
				"attributes": {"motor_id": 31},
			},
			id="body",
		),
		pytest.param(
			{
				"body": "",
				"attributes": {"sentry.message.template": "Door motor recovery failed: %s"},
			},
			id="template_attribute",
		),
	],
)
def test_before_send_sentry_log_drops_door_motor_recovery_failure(log: dict) -> None:
	assert openshelf_os._before_send_sentry_log(log, {}) is None


@pytest.mark.parametrize(
	"log",
	[
		pytest.param({"body": PYMODBUS_RETRY_TEXT}, id="message"),
		pytest.param(
			{
				"body": "",
				"attributes": {"message": "No response received after 12 retries, continue with next request"},
			},
			id="attribute",
		),
		pytest.param({"body": PYMODBUS_FRAME_DUMP}, id="frame_dump"),
		pytest.param({"body": PYMODBUS_REPEATING_FRAME_DUMP}, id="repeating_frame"),
	],
)
def test_before_send_sentry_log_drops_pymodbus_no_response_retry(log: dict) -> None:
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
