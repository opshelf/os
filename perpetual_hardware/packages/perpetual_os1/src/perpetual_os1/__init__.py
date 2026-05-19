import logging
import os
from typing import Any, List, cast

import sentry_sdk

from perpetual_os1.device import get_device_id
from perpetual_os1.operating_mode import get_operating_mode, should_enable_sentry

try:
	from sentry_sdk.integrations.logging import LoggingIntegration
except Exception:  # pragma: no cover - optional dependency guard for tests
	LoggingIntegration = None  # type: ignore[misc, assignment]

sentry_dsn = os.getenv("SENTRY_DSN_HARDWARE", "")
device_id = get_device_id()


def _configure_sentry() -> bool:
	mode = get_operating_mode()
	if should_enable_sentry(mode) is False:
		logging.debug("Sentry disabled for perpetual_os1: mode=%s", mode.value)
		return False

	if sentry_dsn == "":
		logging.warning("Sentry enabled for perpetual_os1 but SENTRY_DSN_HARDWARE is not set")
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
		before_send=_before_send_sentry_event,
		before_send_log=_before_send_sentry_log,
	)
	sentry_sdk.set_tag("device_id", device_id)
	sentry_sdk.set_context("opshelf_device", {"id": device_id})
	logging.info("Sentry initialized for perpetual_os1", mode=mode.value)
	return True

SENSITIVE_HEADER_NAMES = {"authorization", "cookie"}
PYMODBUS_REQUEST_ID_MISMATCH_PREFIX = "ERROR: request ask for id="


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


def _sentry_log_is_pymodbus_request_id_mismatch(log: dict[str, Any]) -> bool:
	for key in ("body", "message"):
		if _is_pymodbus_request_id_mismatch(log.get(key)) is True:
			return True

	attributes = log.get("attributes")
	if isinstance(attributes, dict) is False:
		return False

	return any(_is_pymodbus_request_id_mismatch(value) is True for value in attributes.values())


def _before_send_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if _is_pymodbus_request_id_mismatch(event.get("message")) is True:
		return None

	for entry in event.get("entries", []):
		if isinstance(entry, dict) is False:
			continue

		data = entry.get("data")
		if isinstance(data, dict) is False:
			continue

		if _is_pymodbus_request_id_mismatch(data.get("formatted")) is True:
			return None

	request_value = event.get("request")
	if isinstance(request_value, dict) is True:
		request = cast(dict[str, Any], request_value)
		headers = request.get("headers")
		if isinstance(headers, dict) is True:
			request["headers"] = _redact_sensitive_mapping(headers)

	return event


def _before_send_sentry_log(log: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if _sentry_log_is_pymodbus_request_id_mismatch(log) is True:
		return None

	attributes = log.get("attributes")
	if isinstance(attributes, dict) is True:
		log["attributes"] = _redact_sensitive_mapping(attributes)

	return log


sentry_enabled = _configure_sentry()


def hello() -> str:
	return "Hello from perpetual_os1!"
