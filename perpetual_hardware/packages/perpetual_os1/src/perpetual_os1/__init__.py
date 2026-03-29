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

if sentry_dsn:
	integrations: List[Any] = []
	if LoggingIntegration is not None:
		integrations.append(LoggingIntegration(level=logging.INFO, event_level=logging.ERROR))

	sentry_sdk.init(
		dsn=sentry_dsn,
		traces_sample_rate=0.1,
		environment=sentry_environment,
		server_name=device_id,
		integrations=integrations if integrations else None,  # type: ignore[arg-type]
	)
	sentry_sdk.set_tag("device_id", device_id)
	sentry_sdk.set_context("opshelf_device", {"id": device_id})
	logging.info("Sentry initialized for perpetual_os1")
else:
	logging.debug("Sentry not configured for perpetual_os1: SENTRY_DSN_HARDWARE not set")

def hello() -> str:
    return "Hello from perpetual_os1!"
