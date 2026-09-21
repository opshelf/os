"""The CORS policy must never pair a wildcard origin with credentialed access."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from openshelf_os_server import main
from openshelf_os_server.main import app
from openshelf_os_server.utils.cors_policy import (
	CORS_ORIGINS_ENV,
	allows_credentials,
	resolve_allowed_origins,
)

UI_ORIGIN = "http://device.local:8080"
LOCALHOST_ORIGIN = "http://localhost:5173"
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


def _pinned_app() -> TestClient:
	origins = resolve_allowed_origins(f"{UI_ORIGIN}, {LOCALHOST_ORIGIN}")
	service = FastAPI()
	service.add_middleware(
		CORSMiddleware,
		allow_origins=origins,
		allow_credentials=allows_credentials(origins),
		allow_methods=["*"],
		allow_headers=["*"],
	)

	@service.get("/ping")
	async def ping() -> dict[str, bool]:
		return {"ok": True}

	return TestClient(service)


def test_default_policy_allows_any_origin_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)
	origins = resolve_allowed_origins()
	assert origins == ["*"]
	assert allows_credentials(origins) is False


def test_pinned_origins_replace_the_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv(CORS_ORIGINS_ENV, f"{UI_ORIGIN}, {LOCALHOST_ORIGIN}")
	origins = resolve_allowed_origins()
	assert origins == [UI_ORIGIN, LOCALHOST_ORIGIN]
	assert allows_credentials(origins) is True


def test_pinned_policy_echoes_listed_origins_with_credentials() -> None:
	client = _pinned_app()

	listed = _preflight(client, "/ping", UI_ORIGIN)
	assert listed.status_code in (200, 204)
	assert listed.headers["access-control-allow-origin"] == UI_ORIGIN
	assert listed.headers["access-control-allow-credentials"] == "true"

	unlisted = _preflight(client, "/ping", FOREIGN_ORIGIN)
	assert "access-control-allow-origin" not in unlisted.headers


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
