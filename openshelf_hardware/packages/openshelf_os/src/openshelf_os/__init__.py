import logging
import os
from typing import Any, List, cast

import sentry_sdk

from openshelf_os.device import get_device_id
from openshelf_os.operating_mode import get_operating_mode, should_enable_sentry

try:
	from sentry_sdk.integrations.logging import LoggingIntegration
except Exception:  # pragma: no cover - optional dependency guard for tests
	LoggingIntegration = None  # type: ignore[misc, assignment]

sentry_dsn = os.getenv("SENTRY_DSN_HARDWARE", "")
device_id = get_device_id()


def _configure_sentry() -> bool:
	mode = get_operating_mode()
	if should_enable_sentry(mode) is False:
		logging.debug("Sentry disabled for openshelf_os: mode=%s", mode.value)
		return False

	if sentry_dsn == "":
		logging.warning("Sentry enabled for openshelf_os but SENTRY_DSN_HARDWARE is not set")
		return False

	integrations: List[Any] = []
	if LoggingIntegration is not None:
		integrations.append(
			LoggingIntegration(
				sentry_logs_level=logging.INFO,
				level=logging.INFO,
				event_level=logging.ERROR,
			)
		)

	sentry_sdk.init(
		dsn=sentry_dsn,
		enable_logs=True,
		traces_sample_rate=0.1,
		environment=mode.value,
		server_name=device_id,
		integrations=integrations if integrations else None,  # type: ignore[arg-type]
		before_send=cast(Any, _before_send_sentry_event),
		before_send_log=cast(Any, _before_send_sentry_log),
	)
	sentry_sdk.set_tag("device_id", device_id)
	sentry_sdk.set_context("opshelf_device", {"id": device_id})
	logging.info("Sentry initialized for openshelf_os", extra={"mode": mode.value})
	return True

SENSITIVE_HEADER_NAMES = {"authorization", "cookie"}
PYMODBUS_REQUEST_ID_MISMATCH_PREFIX = "ERROR: request ask for id="
PYMODBUS_NO_RESPONSE_RETRY_PREFIX = "No response received after "
PYMODBUS_NO_RESPONSE_RETRY_SUFFIX = " retries, continue with next request"
# pymodbus' Log.error/Log.critical append the last frames as "\n>>>>> send: ..." /
# "\n>>>>> recv: ..." lines whenever DEBUG logging is off, so the message of a
# no-response retry is the log text plus that trailer. Every appended line
# carries the marker prefix; a deduplicated frame shows up as
# ">>>>> Repeating....".
PYMODBUS_FRAME_DUMP_LINE_PREFIX = ">>>>> "
DOOR_MOTOR_RECOVERY_EVENT = "Too many bad door readings; attempting door motor recovery"
DOOR_MOTOR_RECOVERY_FAILED_PREFIX = "Door motor recovery failed:"


def _redact_sensitive_mapping(value: Any) -> Any:
	if isinstance(value, dict) is True:
		redacted: dict[str, Any] = {}

		for key, item in value.items():
			if isinstance(key, str) is True and key.lower() in SENSITIVE_HEADER_NAMES:
				redacted[key] = "[Filtered]"
				continue

			redacted[key] = _redact_sensitive_mapping(item)

		return redacted

	if isinstance(value, list) is True:
		return [_redact_sensitive_mapping(item) for item in value]

	return value


def _is_pymodbus_request_id_mismatch(message: Any) -> bool:
	return isinstance(message, str) is True and message.startswith(PYMODBUS_REQUEST_ID_MISMATCH_PREFIX)


def _strip_pymodbus_frame_dump(message: str) -> str:
	"""Drop the frame dump pymodbus appends to its error messages.

	``Log.error``/``Log.critical`` add the last frames as ``>>>>> send:`` /
	``>>>>> recv:`` lines when DEBUG logging is off, so only the first line
	carries the log text. Anything else after a newline is left untouched.
	"""
	log_text, separator, trailer = message.partition("\n")
	if separator == "":
		return message

	trailer_lines = [line for line in trailer.split("\n") if line.strip() != ""]
	if trailer_lines == []:
		return message

	if any(
		line.startswith(PYMODBUS_FRAME_DUMP_LINE_PREFIX) is False for line in trailer_lines
	):
		return message

	return log_text


def _is_pymodbus_no_response_retry(message: Any) -> bool:
	if isinstance(message, str) is False:
		return False

	log_text = _strip_pymodbus_frame_dump(message)

	if log_text.startswith(PYMODBUS_NO_RESPONSE_RETRY_PREFIX) is False:
		return False

	if log_text.endswith(PYMODBUS_NO_RESPONSE_RETRY_SUFFIX) is False:
		return False

	retry_count = log_text[
		len(PYMODBUS_NO_RESPONSE_RETRY_PREFIX):-len(PYMODBUS_NO_RESPONSE_RETRY_SUFFIX)
	]
	return retry_count.isdigit()


def _is_suppressed_pymodbus_log_message(message: Any) -> bool:
	return (
		_is_pymodbus_request_id_mismatch(message) is True
		or _is_pymodbus_no_response_retry(message) is True
	)


def _is_door_motor_recovery_failure(message: Any) -> bool:
	return isinstance(message, str) is True and message.startswith(DOOR_MOTOR_RECOVERY_FAILED_PREFIX)


def _sentry_log_is_suppressed_pymodbus_message(log: dict[str, Any]) -> bool:
	for key in ("body", "message"):
		if _is_suppressed_pymodbus_log_message(log.get(key)) is True:
			return True

	attributes_value = log.get("attributes")
	if isinstance(attributes_value, dict) is False:
		return False

	attributes = cast(dict[str, Any], attributes_value)
	return any(_is_suppressed_pymodbus_log_message(value) is True for value in attributes.values())


def _sentry_log_has_message(log: dict[str, Any], target: str) -> bool:
	for key in ("body", "message"):
		if log.get(key) == target:
			return True

	attributes_value = log.get("attributes")
	if isinstance(attributes_value, dict) is False:
		return False

	attributes = cast(dict[str, Any], attributes_value)
	return any(value == target for value in attributes.values())


def _sentry_log_has_door_motor_recovery_failure(log: dict[str, Any]) -> bool:
	# Drop the whole recovery-failure class: the IO-box door recovery is a
	# best-effort self-healing path, and its failure message is logged without a
	# stacktrace, so it would only create log-only Sentry issues.
	for key in ("body", "message"):
		if _is_door_motor_recovery_failure(log.get(key)) is True:
			return True

	attributes_value = log.get("attributes")
	if isinstance(attributes_value, dict) is False:
		return False

	attributes = cast(dict[str, Any], attributes_value)
	return any(_is_door_motor_recovery_failure(value) is True for value in attributes.values())


def _sentry_event_has_message(event: dict[str, Any], target: str) -> bool:
	if event.get("message") == target:
		return True

	logentry_value = event.get("logentry")
	if isinstance(logentry_value, dict) is True:
		logentry = cast(dict[str, Any], logentry_value)
		if any(logentry.get(key) == target for key in ("message", "formatted")) is True:
			return True

	for entry in event.get("entries", []):
		if isinstance(entry, dict) is False:
			continue

		data = entry.get("data")
		if isinstance(data, dict) is False:
			continue

		if any(data.get(key) == target for key in ("message", "formatted")) is True:
			return True

	return False


def _sentry_event_has_door_motor_recovery_failure(event: dict[str, Any]) -> bool:
	if _is_door_motor_recovery_failure(event.get("message")) is True:
		return True

	logentry_value = event.get("logentry")
	if isinstance(logentry_value, dict) is True:
		logentry = cast(dict[str, Any], logentry_value)
		if any(_is_door_motor_recovery_failure(logentry.get(key)) is True for key in ("message", "formatted")):
			return True

	for entry in event.get("entries", []):
		if isinstance(entry, dict) is False:
			continue

		data = entry.get("data")
		if isinstance(data, dict) is False:
			continue

		if any(_is_door_motor_recovery_failure(data.get(key)) is True for key in ("message", "formatted")):
			return True

	return False


def _sentry_event_has_suppressed_pymodbus_message(event: dict[str, Any]) -> bool:
	if _is_suppressed_pymodbus_log_message(event.get("message")) is True:
		return True

	logentry_value = event.get("logentry")
	if isinstance(logentry_value, dict) is True:
		logentry = cast(dict[str, Any], logentry_value)
		if any(_is_suppressed_pymodbus_log_message(logentry.get(key)) is True for key in ("message", "formatted")):
			return True

	for entry in event.get("entries", []):
		if isinstance(entry, dict) is False:
			continue

		data = entry.get("data")
		if isinstance(data, dict) is False:
			continue

		if any(_is_suppressed_pymodbus_log_message(data.get(key)) is True for key in ("message", "formatted")):
			return True

	return False


def _before_send_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if _sentry_event_has_suppressed_pymodbus_message(event) is True:
		return None

	if _sentry_event_has_message(event, DOOR_MOTOR_RECOVERY_EVENT) is True:
		return None

	# A recovery home that fails on a faulted drive is still the expected
	# self-healing path: the door keeps its cached state and the motor fault is
	# still reported separately as the motor's "Fault detected - stopping motor"
	# warning or an ERROR-level motion-timeout event. The recovery message is a
	# stackless log-only issue, so drop the whole recovery-failure class.
	if _sentry_event_has_door_motor_recovery_failure(event) is True:
		return None

	request_value = event.get("request")
	if isinstance(request_value, dict) is True:
		request = cast(dict[str, Any], request_value)
		headers = request.get("headers")
		if isinstance(headers, dict) is True:
			request["headers"] = _redact_sensitive_mapping(headers)

	return event


def _before_send_sentry_log(log: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if _sentry_log_is_suppressed_pymodbus_message(log) is True:
		return None

	# Door position recovery is an expected hardware self-healing path. Keep it in
	# local logs without creating a log-only Sentry issue.
	if _sentry_log_has_message(log, DOOR_MOTOR_RECOVERY_EVENT) is True:
		return None

	if _sentry_log_has_door_motor_recovery_failure(log) is True:
		return None

	attributes = log.get("attributes")
	if isinstance(attributes, dict) is True:
		log["attributes"] = _redact_sensitive_mapping(attributes)

	return log


sentry_enabled = _configure_sentry()
