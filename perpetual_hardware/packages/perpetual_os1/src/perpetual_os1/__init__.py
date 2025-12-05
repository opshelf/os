import logging
import os

import sentry_sdk

try:
	from sentry_sdk.integrations.logging import LoggingIntegration
except Exception:  # pragma: no cover - optional dependency guard for tests
	LoggingIntegration = None  # type: ignore[assignment]

sentry_dsn = os.getenv("SENTRY_DSN_HARDWARE", "")

if sentry_dsn:
	integrations = []
	if LoggingIntegration is not None:
		integrations.append(LoggingIntegration(level=logging.INFO, event_level=logging.ERROR))

	sentry_sdk.init(
		dsn=sentry_dsn,
		traces_sample_rate=0.1,
		environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "development"),
		integrations=integrations or None,
	)
	logging.info("Sentry initialized for perpetual_os1")
else:
	logging.debug("Sentry not configured for perpetual_os1: SENTRY_DSN_HARDWARE not set")

def hello() -> str:
    return "Hello from perpetual_os1!"
