import logging
import os

import sentry_sdk

deployment_environment = os.getenv("ENV", "development")
is_production = deployment_environment == "production"

if is_production is True:
	sentry_sdk.init(
		dsn="https://4a1dffab81cbbe859f982f1f22541a68@o4510181088296960.ingest.us.sentry.io/4510260412809216",
		traces_sample_rate=0.1,
		environment=deployment_environment,
	)
	logging.info("Sentry initialized for perpetual_os1")

def hello() -> str:
    return "Hello from perpetual_os1!"
