import logging
import os
from typing import Any, List

import sentry_sdk

from perpetual_os1.device import get_device_id

try:
	from sentry_sdk.integrations.logging import LoggingIntegration
except Exception:  # pragma: no cover - optional dependency guard for tests
	LoggingIntegration = None  # type: ignore[misc, assignment]

sentry_dsn = os.getenv("SENTRY_DSN_HARDWARE", "")
sentry_environment = os.getenv("SENTRY_ENVIRONMENT", os.getenv("DEPLOYMENT_ENVIRONMENT", "development"))
device_id = get_device_id()

SENSITIVE_HEADER_NAMES = {"authorization", "cookie"}


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


def _before_send_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	request = event.get("request")
	if isinstance(request, dict) is True:
		headers = request.get("headers")
		if isinstance(headers, dict) is True:
			request["headers"] = _redact_sensitive_mapping(headers)

	return event


def _before_send_sentry_log(log: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	attributes = log.get("attributes")
	if isinstance(attributes, dict) is True:
		log["attributes"] = _redact_sensitive_mapping(attributes)

	return log


if sentry_dsn:
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
		environment=sentry_environment,
		server_name=device_id,
		integrations=integrations if integrations else None,  # type: ignore[arg-type]
		before_send=_before_send_sentry_event,
		before_send_log=_before_send_sentry_log,
	)
	sentry_sdk.set_tag("device_id", device_id)
	sentry_sdk.set_context("opshelf_device", {"id": device_id})
	logging.info("Sentry initialized for perpetual_os1")
else:
	logging.debug("Sentry not configured for perpetual_os1: SENTRY_DSN_HARDWARE not set")


def hello() -> str:
	return "Hello from perpetual_os1!"
