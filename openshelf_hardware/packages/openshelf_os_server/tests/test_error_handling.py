import asyncio
import errno

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError, OperationalError

from openshelf_os.config.exceptions import ConfigError
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError
from openshelf_os_server.main import app

TEST_PATH = "/api/test/error-handling"


def _raise_for_kind(kind: str) -> None:
	if kind == "validation":
		raise ValidationError("name is required")
	if kind == "cell_occupied":
		raise CellOccupiedError("cell is occupied by another item")
	if kind == "chemical_not_found":
		raise ChemicalNotFound("no match for cas 123-45-6")
	if kind == "chemical_unavailable":
		raise ChemicalUnavailable("CAS search unavailable")
	if kind == "integrity":
		raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed: user.email"))
	if kind == "db_error":
		raise OperationalError("SELECT * FROM items", (), Exception("no such table: items"))
	if kind == "timeout":
		raise asyncio.TimeoutError("operation timed out")
	if kind == "os_error":
		raise OSError(errno.EIO, "short write to printer")
	if kind == "os_error_filename":
		raise OSError(errno.EIO, "Short write to device", "/dev/usb/lp0")
	if kind == "runtime_error":
		raise RuntimeError("boom")
	if kind == "config_error":
		raise ConfigError("Invalid JSON in /app/config.json")
	if kind == "string_detail":
		raise HTTPException(status_code=404, detail="thing not found")
	if kind == "dict_detail":
		raise HTTPException(
			status_code=409,
			detail={"message": "Home required before robot commands.", "code": "home_required"},
		)
	raise ValueError(f"unknown kind {kind}")


@pytest.mark.anyio
async def _client_with_test_route(unauthenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
	# Keep this test-only route out of subsequent live-route/OpenAPI checks.
	monkeypatch.setattr(app.router, "routes", list(app.router.routes))

	if all(getattr(route, "path", None) != TEST_PATH for route in app.router.routes):

		@app.get(TEST_PATH)
		async def error_handling_route(kind: str) -> None:
			_raise_for_kind(kind)

	return unauthenticated_client


@pytest.mark.anyio
async def test_500_body_derives_code_and_keeps_correlation_id(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)

	response = await client.get(TEST_PATH, params={"kind": "runtime_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["message"] == "Internal server error"
	assert detail["code"] == "internal_error"
	assert "reason" not in detail
	assert "correlation_id" in detail

	response = await client.get(TEST_PATH, params={"kind": "config_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["code"] == "config_error"
	# Config loader messages embed absolute server paths, so the body carries the
	# fixed whitelist text and never str(exc).
	assert detail["message"] == "Invalid device configuration"
	assert "/app/config.json" not in detail["message"]

	response = await client.get(TEST_PATH, params={"kind": "db_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["code"] == "db_error"
	assert detail["message"] == "Internal server error"
	assert "correlation_id" in detail
	# The formatted SQLAlchemy message carries the statement and bound
	# parameters; neither may leak into the client-visible body.
	body = response.text
	assert "[SQL:" not in body
	assert "parameters" not in body
