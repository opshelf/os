"""The CORS policy must never pair a wildcard origin with credentialed access."""

from __future__ import annotations

from starlette.testclient import TestClient

from openshelf_os_server import main
from openshelf_os_server.main import app

FOREIGN_ORIGIN = "http://unlisted.test"
PREFLIGHT_METHOD = "GET"
PREFLIGHT_REQUEST_HEADERS = "authorization,x-correlation-id"


def _preflight(client: TestClient, path: str, origin: str):
	return client.options(
		path,
		headers={
			"Origin": origin,
			"Access-Control-Request-Method": PREFLIGHT_METHOD,
			"Access-Control-Request-Headers": PREFLIGHT_REQUEST_HEADERS,
		},
	)


def test_wildcard_policy_publishes_no_credential_invitation() -> None:
	# The middleware resolves the policy once at import, so this asserts against the
	# policy the app actually runs with rather than a hard-coded environment.
	client = TestClient(app)
	response = _preflight(client, "/api/health", FOREIGN_ORIGIN)

	assert response.status_code in (200, 204)
	if "*" in main.CORS_ALLOWED_ORIGINS:
		assert response.headers["access-control-allow-origin"] == "*"
		assert "access-control-allow-credentials" not in response.headers
	else:
		assert response.headers.get("access-control-allow-origin") != FOREIGN_ORIGIN
