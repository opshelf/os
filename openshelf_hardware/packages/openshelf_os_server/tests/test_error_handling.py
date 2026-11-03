import asyncio
import ast
import errno
import logging
import pathlib
import re
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from hardware.exceptions import ModbusConnectionError, MotorConfigError, MotorError
from openshelf_os.config.exceptions import ConfigError
from openshelf_os.faults import fault_code
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError
from openshelf_os_server.errors import (
	CODE_MESSAGES,
	DEFAULT_STATUS_CODES,
	GENERIC_ERROR_MESSAGE,
	SAFE_500_TYPES,
	api_error,
	hardware_api_error,
	normalize_operator_message,
	standardize_http_detail,
)
from openshelf_os_server.main import app
from openshelf_os_server.middleware import audit

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
		raise ChemicalUnavailable("The chemical lookup is unavailable. Try again.")
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
	if kind == "chained":
		try:
			raise MotorError("Alarm 24 persists after clear attempt on /dev/ttyUSB0")
		except MotorError as exc:
			raise api_error(503, "motor_alarm_clear_failed", "") from exc
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
			detail={"message": "The robot needs to be reset before this can continue.", "code": "home_required"},
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
	# The field problem stays in the server log, tied to the client; the submitted value
	# (a PIN on sign-in) reaches neither.
	records = [r for r in caplog.records if r.name == "openshelf_os_server.errors" and r.levelno == logging.WARNING]
	assert records and "body.barcode" in caplog.text
	assert "not-an-int" not in caplog.text


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
	assert {key: hand_written[key] for key in ("message", "code")} == {"message": "Thing not found.", "code": "not_found"}

	route_detail = (await client.get(TEST_PATH, params={"kind": "codeless_dict"})).json()["detail"]
	assert route_detail["code"] == "conflict"
	assert route_detail["message"] == "Unable to print login barcode."
	# Extra keys a route added for the client keep working.
	assert route_detail["login_barcode_assigned"] is True

	structured = (await client.get(TEST_PATH, params={"kind": "dict_detail"})).json()["detail"]
	assert structured["code"] == "home_required"
	assert structured["message"] == "The robot needs to be reset before this can continue."


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
	detail = response.json()["detail"]
	assert (detail["message"], detail["code"]) == (CODE_MESSAGES["conflict"], "conflict")
	assert set(detail) == {"message", "code", "correlation_id"}
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


@pytest.mark.anyio
async def test_error_bodies_carry_the_correlation_id_never_the_exception(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Driver text carries paths and ids, and a submitted value can be a PIN; none of it
	reaches a client. The correlation id ties the body to the log line that has it."""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)
	requests = (
		("GET", TEST_PATH, {"kind": "chained"}, None),
		("GET", TEST_PATH, {"kind": "os_error_filename"}, None),
		("GET", TEST_PATH, {"kind": "runtime_error"}, None),
		("POST", f"{TEST_PATH}/validate", None, {"barcode": "not-an-int"}),
	)

	for method, path, params, body in requests:
		response = await client.request(method, path, params=params, json=body)
		detail = response.json()["detail"]
		assert set(detail) == {"message", "code", "correlation_id"}, (path, params)
		assert detail["correlation_id"], (path, params)
		for leak in ("/dev/", "Alarm 24", "boom", "not-an-int", "barcode"):
			assert leak not in response.text, (path, params, leak)


@pytest.mark.anyio
async def test_unhandled_500_logs_the_exception_under_its_correlation_id(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""AuditMiddleware answers unhandled exceptions itself, so its log line is where a
	toast's ref leads."""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)
	with caplog.at_level(logging.WARNING, logger="openshelf_os_server.middleware.audit"):
		response = await client.get(TEST_PATH, params={"kind": "runtime_error"})
	assert response.status_code == 500
	ref = response.json()["detail"]["correlation_id"]
	record = next(r for r in caplog.records if r.name == "openshelf_os_server.middleware.audit" and ref in r.getMessage())
	assert record.exc_info is not None and str(record.exc_info[1]) == "boom"


@pytest.mark.anyio
async def test_route_error_logs_the_failure_behind_its_sentence(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""Toasts carry only the sentence and a ref; most routes raise `from exc` without
	logging, so the handler's log line is the only record of the cause."""
	client = await _client_with_test_route(unauthenticated_client, monkeypatch)
	with caplog.at_level(logging.WARNING, logger="openshelf_os_server.errors"):
		response = await client.get(TEST_PATH, params={"kind": "chained"})
	ref = response.json()["detail"]["correlation_id"]
	record = next(r for r in caplog.records if r.name == "openshelf_os_server.errors" and ref in r.getMessage())
	assert record.exc_info is not None and "Alarm 24 persists" in str(record.exc_info[1])


@pytest.mark.anyio
async def test_unhandled_500_on_audit_excluded_route_keeps_correlation_id(
	unauthenticated_client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Routes AuditMiddleware skips (health, laser stream, motor probe) fall through to
	the app-level handler, which must build the same body as the middleware."""
	await _client_with_test_route(unauthenticated_client, monkeypatch)
	monkeypatch.setattr(audit, "ALWAYS_EXCLUDED_ENDPOINTS", frozenset({TEST_PATH}))
	transport = ASGITransport(app=app, raise_app_exceptions=False)

	async with AsyncClient(transport=transport, base_url="http://test") as client:
		anonymous = await client.get(TEST_PATH, params={"kind": "runtime_error"})
		assert anonymous.status_code == 500
	assert anonymous.json()["detail"]["correlation_id"]
	assert "boom" not in anonymous.text


def test_motor_fault_behind_a_catch_all_gets_its_own_code() -> None:
	"""A drive alarm is not cleared by retrying, so the route's "Try again"
	fallback must give way to the code that says what to do."""
	fault = MotorError("Movement failed for motor 3 (status=FAULT)")
	setattr(fault, "fault_context", {"alarm_code": 24, "stall_detected": False, "no_progress": False})
	try:
		try:
			raise fault
		except MotorError as inner:
			raise MotorError("Failed to move motor 3") from inner
	except MotorError as outer:
		error = hardware_api_error(outer, 503, "motor_move_failed", "The robot could not move. Check the robot status before moving it again.")
	assert error.detail["code"] == "motor_alarm"
	assert "try again" not in error.detail["message"].lower()

	plain = hardware_api_error(RuntimeError("boom"), 503, "motor_move_failed", "The robot could not move. Check the robot status before moving it again.")
	assert plain.detail["code"] == "motor_move_failed"


def test_bus_disconnect_beats_fault_context_on_the_outer_error() -> None:
	"""A move attaches fault_context to the outer MotorError even when the cause
	is a lost bus; the operator must be told to check the cable, not a motor fault."""
	context = {"alarm_code": 24, "stall_detected": True, "no_progress": False}
	try:
		try:
			raise ModbusConnectionError("bus lost")
		except ModbusConnectionError as inner:
			outer = MotorError("Movement failed for motor 3")
			setattr(outer, "fault_context", context)
			raise outer from inner
	except MotorError as disconnected:
		assert fault_code(disconnected) == "motor_bus_disconnected"

	alarm = MotorError("Movement failed for motor 3 (status=FAULT)")
	setattr(alarm, "fault_context", context)
	try:
		raise MotorError("Failed to move motor 3") from alarm
	except MotorError as outer_alarm:
		assert fault_code(outer_alarm) == "motor_alarm"


def test_root_cause_beats_config_wrapper() -> None:
	"""Homing and configure wrap motion faults in MotorConfigError; the root cause wins."""
	stall = MotorError("Movement failed for motor 3")
	setattr(stall, "fault_context", {"alarm_code": 0, "stall_detected": True, "no_progress": False})
	try:
		try:
			raise stall
		except MotorError as inner:
			raise MotorConfigError("Homing failed for motor 3") from inner
	except MotorConfigError as wrapped_stall:
		assert fault_code(wrapped_stall) == "motor_stalled"

	try:
		try:
			raise ModbusConnectionError("laser port lost")
		except ModbusConnectionError as inner:
			raise MotorConfigError("Configure failed") from inner
	except MotorConfigError as wrapped_bus:
		assert fault_code(wrapped_bus) == "motor_bus_disconnected"

	assert fault_code(MotorConfigError("bad register value")) == "config_error"
	assert "device on the machine bus" in CODE_MESSAGES["motor_bus_disconnected"]


def test_every_derived_code_has_its_own_sentence() -> None:
	"""A code without copy would ship the generic sentence instead of its own."""
	derived = {code for _, code in SAFE_500_TYPES} | set(DEFAULT_STATUS_CODES.values())
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
					and node.func.id in {"api_error", "error_detail", "hardware_api_error"}
				):
					code_index = 2 if node.func.id == "hardware_api_error" else 1
					if (
						len(node.args) > code_index
						and isinstance(node.args[code_index], ast.Constant)
						and isinstance(node.args[code_index].value, str)
					):
						codes.add(node.args[code_index].value)
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


# Exceptions whose text is hand-written operator copy (the kiosk matches some of
# these sentences exactly), and route handlers whose manager raises only such
# sentences as that exception type.
_OPERATOR_COPY_EXCEPTIONS = {
	"BinWorkflowConflict",
	"CycleTestStartError",
	"NoCompatibleBinError",
	"ValidationError",
}
_OPERATOR_COPY_ROUTES = {
	"abort_laser_auto_calibration": {"RuntimeError"},
	"stream_laser_auto_calibration": {"RuntimeError"},
}
# Call name -> position of its client-visible message argument.
_MESSAGE_ARGUMENT = {
	"api_error": 2,
	"hardware_api_error": 3,
	"error_detail": 0,
	"_build_error_detail": 0,
	"_raise_motor_error": 1,
}


def _raw_exception_messages(tree: ast.AST) -> list[str]:
	found: list[str] = []

	def visit(node: ast.AST, function: str) -> None:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			function = node.name
		if isinstance(node, ast.ExceptHandler) and node.name is not None:
			caught = {n.id if isinstance(n, ast.Name) else getattr(n, "attr", "") for n in ast.walk(node.type)} if node.type else set()
			if caught.isdisjoint(_OPERATOR_COPY_EXCEPTIONS | _OPERATOR_COPY_ROUTES.get(function, set())):
				for call in (n for statement in node.body for n in ast.walk(statement) if isinstance(n, ast.Call)):
					name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
					messages = [k.value for k in call.keywords if k.arg == "detail"] if name == "HTTPException" else []
					if name in _MESSAGE_ARGUMENT and len(call.args) > _MESSAGE_ARGUMENT[name]:
						messages.append(call.args[_MESSAGE_ARGUMENT[name]])
					for message in messages:
						if any(isinstance(n, ast.Name) and n.id == node.name for n in ast.walk(message)):
							found.append(f"{function}:{call.lineno}")
		for child in ast.iter_child_nodes(node):
			visit(child, function)

	visit(tree, "")
	return found


def test_router_messages_never_carry_raw_exception_text() -> None:
	"""Exception text belongs in the log; in the message it reaches the kiosk, which
	shows detail.message verbatim."""
	router_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "openshelf_os_server" / "routers"
	found = []
	for path in sorted(router_dir.glob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		found += [f"{path.name}:{site}" for site in _raw_exception_messages(tree)]
	assert found == []


def test_every_http_error_carries_a_code() -> None:
	"""A string detail gets only its status default code, so clients fall back to
	matching the sentence, which breaks on the next copy edit. Raise api_error, or
	build the detail with error_detail."""
	source_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "openshelf_os_server"
	uncoded = []
	for path in sorted(source_dir.rglob("*.py")):
		if path.name == "errors.py":
			continue
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
			if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "HTTPException"):
				continue
			detail = next((k.value for k in node.keywords if k.arg == "detail"), node.args[1] if len(node.args) > 1 else None)
			coded = isinstance(detail, ast.Call) or (
				isinstance(detail, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "code" for k in detail.keys)
			)
			if coded is False:
				uncoded.append(f"{path.relative_to(source_dir)}:{node.lineno}")
	assert uncoded == []


def test_route_messages_do_not_restate_their_code_sentence() -> None:
	"""CODE_MESSAGES is the one copy per code; a route that has nothing to add passes ""
	and gets it. A restated copy drifts from the served sentence on the next edit, so a
	route literal must add a word its code sentence lacks (a name, a reason, a step)."""
	source_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "openshelf_os_server"
	positions = {"api_error": (1, 2), "hardware_api_error": (2, 3), "error_detail": (1, 0), "CycleTestStartError": (0, 1)}

	def words(text: str) -> set[str]:
		return set(re.findall(r"[a-z0-9']+", text.lower())) - {"a", "an", "the", "this", "that"}

	def const(node: ast.AST | None) -> Any:
		return node.value if isinstance(node, ast.Constant) else None

	restated = []
	for path in sorted(source_dir.rglob("*.py")):
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
			pairs = []
			if isinstance(node, ast.Call):
				name = getattr(node.func, "id", getattr(node.func, "attr", None))
				if name in positions and len(node.args) > max(positions[name]):
					pairs.append(tuple(const(node.args[index]) for index in positions[name]))
			elif isinstance(node, ast.Dict):
				entries = {const(key): const(value) for key, value in zip(node.keys, node.values)}
				pairs.append((entries.get("code"), entries.get("message")))
			for code, message in pairs:
				if isinstance(code, str) and isinstance(message, str) and message != "":
					if words(message) <= words(CODE_MESSAGES.get(code, "")):
						restated.append(f"{path.relative_to(source_dir)}:{node.lineno} {code}")
	assert restated == []


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
