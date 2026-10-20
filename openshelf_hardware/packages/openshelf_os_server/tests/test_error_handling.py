import asyncio
import ast
import errno
import logging
import pathlib

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from openshelf_os.config.exceptions import ConfigError
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError
from openshelf_os_server.errors import (
	CODE_MESSAGES,
	DEFAULT_STATUS_CODES,
	GENERIC_ERROR_MESSAGE,
	SAFE_500_TYPES,
	normalize_operator_message,
	standardize_http_detail,
)
from openshelf_os_server.main import app

TEST_PATH = "/api/test/error-handling"


class _ErrorPayload(BaseModel):
	barcode: int


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
	if kind == "traceback_detail":
		raise HTTPException(
			status_code=400,
			detail='Traceback (most recent call last):\n  File "/app/worker.py", line 12, in run\nTypeError: bad',
		)
	if kind == "path_detail":
		raise HTTPException(status_code=503, detail="Failed to read scale: no response on /dev/ttyUSB1")
	if kind == "empty_detail":
		raise HTTPException(status_code=409, detail="")
	if kind == "bare_code":
		raise HTTPException(status_code=503, detail="hardware_io")
	if kind == "codeless_dict":
		raise HTTPException(
			status_code=409,
			detail={"message": "Unable to print login barcode", "login_barcode_assigned": True},
		)
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

	if all(getattr(route, "path", None) not in {TEST_PATH, f"{TEST_PATH}/validate"} for route in app.router.routes):

		@app.get(TEST_PATH)
		async def error_handling_route(kind: str) -> None:
			_raise_for_kind(kind)

		@app.post(f"{TEST_PATH}/validate")
		async def validation_error_route(payload: _ErrorPayload) -> None:
			return None

	return unauthenticated_client


@pytest.mark.anyio
async def test_request_validation_error_is_standardized(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""FastAPI's default 422 body is a raw list of field problems; the shared
	serializer must replace it so no field path or submitted value reaches a
	client.
	"""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)

	with caplog.at_level(logging.WARNING, logger="openshelf_os_server.errors"):
		response = await client.post(f"{TEST_PATH}/validate", json={"barcode": "not-an-int"})

	assert response.status_code == 422
	detail = response.json()["detail"]
	assert detail["code"] == "validation_failed"
	assert detail["message"] == CODE_MESSAGES["validation_failed"]
	# The raw list of problems never reaches the client.
	assert "loc" not in response.text and "input" not in response.text
	assert "not-an-int" not in response.text
	# The structured problems stay in the server log, tied to the client.
	records = [r for r in caplog.records if r.name == "openshelf_os_server.errors" and r.levelno == logging.WARNING]
	assert records and "not-an-int" in caplog.text


@pytest.mark.anyio
async def test_client_message_is_one_sentence_without_server_detail(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Both client surfaces show ``detail.message`` verbatim, so nothing that only a
	server log can use may travel in it: not database text, device paths, driver
	tracebacks, and not a bare code word where a sentence belongs.
	"""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)

	for kind in (
		"integrity",
		"os_error_filename",
		"traceback_detail",
		"path_detail",
		"empty_detail",
		"bare_code",
	):
		body = (await client.get(TEST_PATH, params={"kind": kind})).json()["detail"]
		message = body["message"]
		assert message.strip() == message and message.endswith("."), (kind, message)
		assert message.lower() != body["code"], (kind, message)
		assert "UNIQUE constraint" not in message, (kind, message)
		assert "/dev/" not in message and "/app/" not in message, (kind, message)
		assert "Traceback" not in message and "line " not in message, (kind, message)

	conflict = (await client.get(TEST_PATH, params={"kind": "integrity"})).json()["detail"]
	assert conflict["code"] == "conflict"
	assert conflict["message"] == CODE_MESSAGES["conflict"]

	unreachable = (await client.get(TEST_PATH, params={"kind": "bare_code"})).json()["detail"]
	assert unreachable["code"] == "service_unavailable"
	# The code word the route sent says more than the status default, so it wins.
	assert unreachable["message"] == CODE_MESSAGES["hardware_io"]

	hand_written = (await client.get(TEST_PATH, params={"kind": "string_detail"})).json()["detail"]
	assert hand_written == {"message": "Thing not found.", "code": "not_found"}

	route_detail = (await client.get(TEST_PATH, params={"kind": "codeless_dict"})).json()["detail"]
	assert route_detail["code"] == "conflict"
	assert route_detail["message"] == "Unable to print login barcode."
	# Extra keys a route added for the client keep working.
	assert route_detail["login_barcode_assigned"] is True

	structured = (await client.get(TEST_PATH, params={"kind": "dict_detail"})).json()["detail"]
	assert structured["code"] == "home_required"
	assert structured["message"] == "Home required before robot commands."


def test_non_string_dict_message_never_reaches_client_as_repr() -> None:
	for message in ({"field": "name", "errors": ["required"]}, 42):
		exc = HTTPException(status_code=409, detail={"message": message, "code": "conflict", "cell": "A1"})
		detail = standardize_http_detail(exc)
		assert detail["message"] == CODE_MESSAGES["conflict"], (message, detail)
		assert "{" not in detail["message"] and "}" not in detail["message"], (message, detail)
		assert detail["code"] == "conflict" and detail["cell"] == "A1", (message, detail)


def test_normalized_message_is_stable_under_renormalization() -> None:
	"""HTTPException details are normalized twice, so a second pass must not change them."""
	for message in (
		"Could not open /var/lib/openshelf/conf.json for writing",
		"File 'worker.py', line 12, in run",
		"hardware_io",
		"Thing not found",
	):
		once = normalize_operator_message(message, "conflict")
		assert normalize_operator_message(once, "conflict") == once, (message, once)
		assert "line " not in once and "/var/" not in once, (message, once)

	assert normalize_operator_message(
		"Could not open /var/lib/openshelf/conf.json for writing", "conflict"
	) == "Could not open for writing."

	boot = normalize_operator_message("Failed to write /boot/firmware/wifi.cfg", "config_error")
	assert "/" not in boot and "wifi.cfg" not in boot, boot
	assert normalize_operator_message(boot, "config_error") == boot
	for kept in ("Could not read data/config.json", "Item stuck in tray/bin"):
		assert normalize_operator_message(kept, "conflict") == f"{kept}."


def test_path_scrub_falls_back_when_removal_dangles_a_function_word() -> None:
	"""A function word that lost its object to the scrub would leave an
	ungrammatical sentence, so the code's own sentence stands in.
	"""
	for message in (
		"Could not read the scale reading from /dev/ttyUSB0",
		"Retry the movement after clearing /dev/usb/lp0",
		"Failed on /proc/self/status while polling",
	):
		once = normalize_operator_message(message, "hardware_io")
		assert once == CODE_MESSAGES["hardware_io"], (message, once)
		assert normalize_operator_message(once, "hardware_io") == once


def test_identifier_first_token_stays_uncased() -> None:
	"""An identifier token (an underscore before its end) is left uncased in
	either case; a trailing underscore is an ordinary word and gets
	sentence-cased like the rest of the text.
	"""
	for message, expected in (
		("shelf_idx must be non-negative", "shelf_idx must be non-negative."),
		("Z_EXTENSION_PLACE not configured", "Z_EXTENSION_PLACE not configured."),
		("shelf_ a", "Shelf_ a."),
		("cell_ x", "Cell_ x."),
		("some ordinary text", "Some ordinary text."),
	):
		assert normalize_operator_message(message, "config_error") == expected, message


@pytest.mark.anyio
async def test_500_body_derives_code_and_keeps_correlation_id(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)

	response = await client.get(TEST_PATH, params={"kind": "runtime_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["message"] == CODE_MESSAGES["internal_error"]
	assert detail["code"] == "internal_error"
	assert "reason" not in detail
	assert "correlation_id" in detail

	response = await client.get(TEST_PATH, params={"kind": "config_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["code"] == "config_error"
	# Config loader messages embed absolute server paths, so the body carries the
	# code's sentence and never str(exc).
	assert detail["message"] == CODE_MESSAGES["config_error"]
	assert "/app/config.json" not in detail["message"]

	response = await client.get(TEST_PATH, params={"kind": "db_error"})
	assert response.status_code == 500
	detail = response.json()["detail"]
	assert detail["code"] == "db_error"
	assert detail["message"] == CODE_MESSAGES["db_error"]
	assert "correlation_id" in detail
	# The formatted SQLAlchemy message carries the statement and bound
	# parameters; neither may leak into the client-visible body.
	body = response.text
	assert "[SQL:" not in body
	assert "parameters" not in body


@pytest.mark.anyio
async def test_integrity_conflict_logs_constraint_but_body_stays_generic(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""An unexpected constraint violation must stay diagnosable on a device."""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)

	with caplog.at_level(logging.WARNING, logger="openshelf_os_server.errors"):
		response = await client.get(TEST_PATH, params={"kind": "integrity"})

	assert response.status_code == 409
	assert response.json()["detail"] == {"message": CODE_MESSAGES["conflict"], "code": "conflict"}
	assert "UNIQUE constraint failed: user.email" not in response.text
	records = [r for r in caplog.records if r.name == "openshelf_os_server.errors" and r.levelno == logging.WARNING]
	assert records and records[-1].exc_info is not None
	assert "UNIQUE constraint failed: user.email" in caplog.text


def test_bare_code_word_never_reaches_client_as_identifier() -> None:
	emitted = {
		"home_required",
		"movement_in_progress",
		"cell_not_found",
		"cell_unavailable",
		"cell_too_small",
		"invalid_location",
		"reserved_bin_barcode",
		"rotation_blocked",
		"bin_workflow_conflict",
		"scale_not_enabled",
	}
	unknown = {
		"brand_new_code",
		"widget",
		"CamelCase",
		"HTTP",
	}
	for value in emitted | CODE_MESSAGES.keys() | unknown:
		message = normalize_operator_message(value, "internal_error")
		assert message == CODE_MESSAGES.get(value.lower(), GENERIC_ERROR_MESSAGE), value
		assert message[0].isupper() and message.endswith("."), value


def test_every_derived_code_has_its_own_sentence() -> None:
	"""A code without copy would ship the generic sentence instead of its own."""
	derived = {code for _, code, _ in SAFE_500_TYPES} | set(DEFAULT_STATUS_CODES.values())
	assert derived - CODE_MESSAGES.keys() == set()


def _emitted_router_codes() -> set[str]:
	"""Extract every code literal a router call sets.

	Covers the shapes that occur in the routers: the code parameter
	(`api_error(status, code, ...)` and `error_detail(message, code, ...)`),
	a `code="..."` keyword (`_build_error_detail(..., code=...)`), and a
	`"code": "..."` key in a dict detail.
	"""
	router_dir = (
		pathlib.Path(__file__).resolve().parents[1]
		/ "src"
		/ "openshelf_os_server"
		/ "routers"
	)
	codes: set[str] = set()
	for path in sorted(router_dir.glob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		for node in ast.walk(tree):
			if isinstance(node, ast.Call):
				if (
					isinstance(node.func, ast.Name)
					and node.func.id in {"api_error", "error_detail"}
					and len(node.args) >= 2
					and isinstance(node.args[1], ast.Constant)
					and isinstance(node.args[1].value, str)
				):
					codes.add(node.args[1].value)
				for keyword in node.keywords:
					if (
						keyword.arg == "code"
						and isinstance(keyword.value, ast.Constant)
						and isinstance(keyword.value.value, str)
					):
						codes.add(keyword.value.value)
			elif isinstance(node, ast.Dict):
				for key, value in zip(node.keys, node.values):
					if (
						isinstance(key, ast.Constant)
						and key.value == "code"
						and isinstance(value, ast.Constant)
						and isinstance(value.value, str)
					):
						codes.add(value.value)
	return codes


def test_every_router_emitted_code_has_a_code_message() -> None:
	"""A code a router raises must carry its own sentence, or a code-first
	client falls back to a status default that is wrong advice for a 409 or
	a 503.
	"""
	missing = _emitted_router_codes() - CODE_MESSAGES.keys()
	assert missing == set(), f"router-emitted codes without a CODE_MESSAGES entry: {sorted(missing)}"


def test_code_messages_follow_operator_copy_rule() -> None:
	for code, message in CODE_MESSAGES.items():
		assert message[0].isupper() and message.endswith("."), code
		assert "please" not in message.lower(), code
