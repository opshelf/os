"""Shared API error construction and exception-to-status mapping.

Routes build HTTP errors with :func:`api_error` so every error body carries a
stable machine-readable ``code``. Exception types that represent client input
problems or known device states (rather than internal bugs) are registered on
the app with :func:`register_mapped_handlers`, so they become typed responses
without per-route ``try/except`` conversion. Starlette resolves the most
specific registered handler via the exception MRO, so subclasses keep their own
more specific mapping. ``RequestValidationError`` is registered there as well,
so FastAPI's raw list-of-field-problems 422 body is replaced by the shared
serializer instead of reaching the client.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, cast

import sentry_sdk
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from openshelf_os.config.exceptions import ConfigError
from openshelf_os.exceptions import ConfigError as HardwareConfigError
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError

logger = logging.getLogger(__name__)

# Exception types that are not code bugs. In an unhandled 500 they get a
# derived code and, when listed, str(exc) as the message; everything else gets
# the CODE_MESSAGES sentence for its code. Full detail remains in the log and
# Sentry, tied to the client by the correlation id.
SAFE_500_TYPES: tuple[tuple[type[BaseException], str, bool], ...] = (
	# (exception type, code, surface str(exc)?)
	# surface str=True -> str(exc), or the code's sentence when empty
	# surface str=False -> the code's sentence
	# (str(exc) is not safe to surface: config loader
	# messages embed absolute paths; SQLAlchemy messages
	# embed the statement and bound parameters)
	(TimeoutError, "timeout", True),
	(ConfigError, "config_error", False),
	(HardwareConfigError, "config_error", False),
	(SQLAlchemyError, "db_error", False),
)


def internal_error_detail(exc: BaseException) -> tuple[str, str]:
	"""Return the (code, message) for an unhandled exception's 500 body."""
	for exc_type, code, surface_str in SAFE_500_TYPES:
		if isinstance(exc, exc_type):
			sentence = CODE_MESSAGES.get(code, GENERIC_ERROR_MESSAGE)
			return code, (str(exc) or sentence) if surface_str else sentence
	return "internal_error", CODE_MESSAGES["internal_error"]


def capture_unhandled_exception(
	exc: BaseException,
	path: str,
	method: str,
	correlation_id: str | None,
	*,
	enabled: bool,
) -> None:
	"""Report an unhandled request exception to Sentry.

	The caller supplies the API-side enable decision; the SDK can also be
	initialized by the hardware side, so ``is_initialized`` alone is not a
	valid gate.
	"""
	if enabled is False or sentry_sdk.is_initialized() is False:
		return
	with sentry_sdk.new_scope() as scope:
		if correlation_id:
			scope.set_tag("correlation_id", correlation_id)
		scope.set_tag("path", path)
		scope.set_tag("method", method)
		sentry_sdk.capture_exception(exc)


def request_correlation_id(request: Request) -> str | None:
	# CorrelationIdMiddleware stores the id on request.state (shared through the
	# scope) so layers outside the middleware can include it in error bodies.
	return getattr(request.state, "correlation_id", None)

# Default code per status, used when a string detail is standardized and the
# route did not supply its own code.
DEFAULT_STATUS_CODES: dict[int, str] = {
	status.HTTP_400_BAD_REQUEST: "bad_request",
	status.HTTP_401_UNAUTHORIZED: "unauthorized",
	status.HTTP_403_FORBIDDEN: "forbidden",
	status.HTTP_404_NOT_FOUND: "not_found",
	status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
	status.HTTP_409_CONFLICT: "conflict",
	status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_failed",
	status.HTTP_429_TOO_MANY_REQUESTS: "queue_full",
	status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
	status.HTTP_502_BAD_GATEWAY: "bad_gateway",
	status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
	status.HTTP_504_GATEWAY_TIMEOUT: "timeout",
}


# Text shown when a message arrived empty, was a bare code word, or turned out
# to be server-side detail with nothing operator-usable left in it.
CODE_MESSAGES: dict[str, str] = {
	"bad_request": "The cabinet did not accept that request.",
	"bad_gateway": "The cabinet did not answer. Try again.",
	"bin_workflow_conflict": "The tray is not ready for that step. Follow the instruction in the message.",
	"cabinet_not_configured": "This cabinet cannot open its doors.",
	"cabinet_release_control_failed": "The cabinet did not release. Try again.",
	"cabinet_status_failed": "The cabinet status could not be read. Try again.",
	"camera_not_found": "That camera is not set up on this cabinet.",
	"camera_unavailable": "That camera is not delivering frames.",
	"cell_not_found": "That shelf spot does not exist.",
	"cell_status_invalid": "That shelf status is not valid.",
	"cell_too_small": "The item does not fit in that shelf spot. Choose a larger one.",
	"cell_unavailable": "That shelf spot is not available. Choose another one.",
	"chemical_not_found": "No matching chemical was found.",
	"chemical_unavailable": "Chemical lookup is not available right now. Try again in a moment.",
	"conflict": "That action does not fit what the cabinet is doing right now.",
	"config_error": "The cabinet settings are not valid.",
	"db_error": "The cabinet could not save the change. Try again.",
	"dimension_invalid": "Enter a size greater than zero.",
	"door_control_failed": "The cabinet door did not move. Try again.",
	"error": "The cabinet could not complete the action.",
	"forbidden": "You do not have permission to do that.",
	"frame_encode_failed": "The IO box camera image could not be read. Try again.",
	"grid_load_failed": "The shelf layout could not be read. Try again.",
	"grid_update_failed": "The shelf layout could not be saved. Try again.",
	"hardware_io": "A part of the cabinet did not respond.",
	"home_required": "The cabinet needs to home first. Home the cabinet and try again.",
	"internal_error": "The cabinet could not complete the action.",
	"invalid_location": "That shelf spot is not valid.",
	"iobox_sizer_unavailable": "The IO box measure tool is not available. Try again.",
	"item_not_found": "There is no item in that shelf spot.",
	"method_not_allowed": "That request is not supported.",
	"motor_alarm_clear_failed": "The cabinet alarm could not be cleared. Try again.",
	"motor_fault": "The cabinet stopped with a motor fault. Home the cabinet and try again.",
	"motor_home_failed": "The cabinet could not be reset. Try again.",
	"motor_move_failed": "The cabinet could not move. Try again.",
	"motor_not_configured": "That motor is not configured on this cabinet.",
	"movement_in_progress": "The cabinet is moving. Wait for the move to finish.",
	"not_found": "That entry is no longer there.",
	"onboarding_state_unavailable": "The cabinet onboarding state is not available. Try again.",
	"power_cycle_failed": "The cabinet could not power cycle that part. Try again.",
	"power_rail_not_configured": "That power rail is not configured on this cabinet.",
	"queue_full": "The cabinet is busy. Wait for the current move to finish.",
	"request_invalid": "That command is not valid for the cabinet right now. Check the shelf spot and try again.",
	"request_replay": "That request was already sent. Reload and try again.",
	"reserved_bin_barcode": "That barcode belongs to a cabinet bin. Scan the item barcode instead.",
	"reserved_tray_item": "That item is already stored in a bin. Use the bin workflow instead.",
	"rotation_blocked": "The cabinet cannot rotate right now. Clear the cabinet and try again.",
	"safety_interruption_active": "The cabinet has an active safety interruption. Clear the listed source and try again.",
	"scale_calibration_failed": "The scale could not be calibrated. Try again.",
	"scale_config_failed": "The scale is not available on this cabinet.",
	"scale_not_enabled": "The cabinet scale is turned off in the settings.",
	"scale_read_failed": "Could not read the scale. Try again.",
	"scale_settings_failed": "The scale settings could not be saved. Try again.",
	"scale_tare_failed": "The scale could not be zeroed. Try again.",
	"service_unavailable": "The cabinet is not ready. Try again in a moment.",
	"target_in_flight": "Another queued or running command already targets this item or cell.",
	"timeout": "The cabinet took too long to respond. Try again.",
	"tray_config_conflict": "The tray settings conflict with stored items. Check the tray barcode and settings.",
	"tray_fetch_failed": "The tray could not be fetched. Try again.",
	"tray_return_failed": "The tray could not be returned. Try again.",
	"unauthorized": "Sign in again.",
	"validation_failed": "Some details are not valid.",
	"wifi_connect_failed": "The cabinet could not connect to that network. Try again.",
	"wifi_connect_failed_offline": "The cabinet could not connect and may now be offline.",
	"wifi_list_failed": "The network list could not be read. Try again.",
	"wifi_unavailable": "Network settings are not available on this cabinet.",
}

GENERIC_ERROR_MESSAGE = CODE_MESSAGES["error"]

# Text that is never worth showing: a traceback, a formatted statement, or a
# stack frame. The server log and Sentry keep the real detail.
_TECHNICAL_MARKERS = ("Traceback (most recent call last)", "[SQL:", '.py", line ', ".py', line ")

# Absolute paths on the device: a slash not glued to a preceding word, then a
# lowercase root (or macOS /Users), so "3/4 inch", "tray/bin" and relative
# paths like data/config.json are left alone.
_SERVER_PATH = re.compile(
	r"(?<![\w./])['\"]?/(?:[a-z][\w.@+-]*|Users)(?:/[\w.@+-]+)*/?['\"]?"
)

# A leading identifier token, e.g. "Z_EXTENSION_PLACE not configured" or
# "shelf_idx must be non-negative". An identifier token carries an underscore
# before its end; a trailing underscore ("shelf_ a") is an ordinary word and
# gets sentence-cased like the rest of the text.
_IDENTIFIER_START = re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+(?=\s|$)")

# The whole message is one identifier: a route sent a code word, not a sentence.
_CODE_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Function words left dangling when the path that was their object is removed
# ("reading from /dev/ttyUSB0", "failed on /proc/self/status"). "to" is also
# an infinitive marker ("failed to write"), so it only dangles as the final
# word or directly before the removed path.
_DANGLING_WORDS = frozenset({
	"a", "an", "and", "at", "before", "for", "from", "in", "on", "to", "the", "with",
	"after",
})
# Prepositions that may stand one content word away from their object
# ("after clearing /dev/usb/lp0"); articles and "to" never do.
_DANGLING_PREPOSITIONS = frozenset({
	"after", "at", "before", "for", "from", "in", "on", "with",
})


def _path_removal_dangles(words: list[str], preceding: list[tuple[str, str]]) -> bool:
	"""True when removing the path left a function word without its object."""
	if len(words) == 0 or words[-1].lower() in _DANGLING_WORDS:
		return True
	for immediate, one_before in preceding:
		if immediate in _DANGLING_WORDS or one_before in _DANGLING_PREPOSITIONS:
			return True
	return False


def normalize_operator_message(message: str, code: str) -> str:
	"""Return one sentence-case operator sentence for a client-visible message.

	Messages reach here from routes, mapped exceptions and 500 handlers, so the
	input ranges from hand-written copy to a driver exception. Client text is
	always a sentence; the technical detail stays in the log, tied to the client
	by the correlation id.
	"""
	fallback = CODE_MESSAGES.get(code, GENERIC_ERROR_MESSAGE)
	text = " ".join((message or "").split("\n")[0].split())
	if text == "":
		return fallback
	if _CODE_WORD.match(text) is not None:
		# A route that had nothing to say sent a code as the message.
		return CODE_MESSAGES.get(text.lower(), fallback)
	if any(marker in text for marker in _TECHNICAL_MARKERS):
		return fallback
	if _SERVER_PATH.search(text) is not None:
		# Words immediately before each removed path, so a function word that
		# lost its object ("reading from /dev/ttyUSB0") is still detectable
		# after the path is gone.
		preceding = []
		for match in _SERVER_PATH.finditer(text):
			before = text[: match.start()].split()
			preceding.append(
				(
					before[-1].lower() if before else "",
					before[-2].lower() if len(before) >= 2 else "",
				)
			)
		text = " ".join(_SERVER_PATH.sub("", text).split()).strip(":,-;'\" ")
		if len(text) < 4:
			return fallback
		if _path_removal_dangles(text.split(), preceding):
			return fallback
	if _IDENTIFIER_START.match(text) is None:
		text = text[0].upper() + text[1:]
	if not text.endswith((".", "?", "!")):
		text = f"{text}."
	return text


def error_detail(message: str, code: str, **extra: Any) -> dict[str, Any]:
	"""Build the structured error body: ``{"message": ..., "code": ...}``."""
	detail: dict[str, Any] = {"message": normalize_operator_message(message, code), "code": code}
	detail.update(extra)
	return detail


def api_error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
	"""Build an ``HTTPException`` whose detail carries message and code."""
	return HTTPException(status_code=status_code, detail=error_detail(message, code, **extra))


def standardize_http_detail(exc: StarletteHTTPException) -> Any:
	"""Wrap string details into the structured form; dict/list pass through.

	A dict detail keeps its own keys but always ends up with a usable ``message``
	and ``code``, because both the kiosk and the developer UI show them as they
	arrive.
	"""
	if isinstance(exc.detail, str):
		return error_detail(exc.detail, DEFAULT_STATUS_CODES.get(exc.status_code, "error"))
	if isinstance(exc.detail, dict):
		detail = dict(exc.detail)
		code = str(detail.get("code") or DEFAULT_STATUS_CODES.get(exc.status_code, "error"))
		detail["code"] = code
		message = detail.get("message")
		# A non-string message is not operator copy; its repr must not reach the wire.
		detail["message"] = normalize_operator_message(message if isinstance(message, str) else "", code)
		return detail
	return exc.detail


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
	# Mirrors Starlette's default HTTPException handler, but standardizes
	# string details into the structured {message, code} form.
	http_exc = cast(StarletteHTTPException, exc)
	return JSONResponse(
		content={"detail": standardize_http_detail(http_exc)},
		status_code=http_exc.status_code,
		headers=http_exc.headers or {},
	)


# Exception types that map to a fixed status and code. These are expected
# conditions (client input problems, missing hardware, state conflicts), not
# bugs: their message is safe to surface to the client.
MAPPED_EXCEPTIONS: list[tuple[type[Exception], int, str]] = [
	(CellOccupiedError, status.HTTP_409_CONFLICT, "conflict"),
	(ValidationError, status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_failed"),
	(ChemicalNotFound, status.HTTP_404_NOT_FOUND, "chemical_not_found"),
	(ChemicalUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE, "chemical_unavailable"),
	(IntegrityError, status.HTTP_409_CONFLICT, "conflict"),
	(asyncio.TimeoutError, status.HTTP_504_GATEWAY_TIMEOUT, "timeout"),
	(OSError, status.HTTP_503_SERVICE_UNAVAILABLE, "hardware_io"),
]

# Extra response headers per mapped code; the unavailable-chemical 503 keeps
# the Retry-After hint clients relied on before this mapping.
MAPPED_HEADERS: dict[str, dict[str, str]] = {"chemical_unavailable": {"Retry-After": "5"}}


def _clean_message(exc: BaseException) -> str:
	# SQLAlchemy DBAPIError messages carry a formatted [SQL: ...] tail and the
	# database text names tables and columns, so the client gets the conflict
	# sentence; the mapped handler logs the exception with its traceback.
	if isinstance(exc, IntegrityError):
		return CODE_MESSAGES["conflict"]
	# OSError str() prefixes the errno, e.g. "[Errno 5] short write to printer",
	# and appends the offending path, e.g. "...: '/dev/usb/lp0'". strerror is the
	# readable part; the path is a server-side detail.
	if isinstance(exc, OSError):
		if getattr(exc, "filename", None) is not None:
			return exc.strerror or CODE_MESSAGES["hardware_io"]
		message = re.sub(r"^\[Errno \d+\] ", "", str(exc))
		return message or CODE_MESSAGES["hardware_io"]
	return str(exc) or CODE_MESSAGES["error"]


def _make_mapped_handler(status_code: int, code: str):
	async def handler(request: Request, exc: BaseException) -> JSONResponse:
		message = _clean_message(exc)
		# These mapped types never reach the 500 fallback, so the server-side
		# diagnostic they logged before the mapping has to be emitted here.
		# Client-input and normal-lookup outcomes (422/404) stay unlogged.
		# A conflict is a handled client outcome: keep it out of Sentry, whose
		# LoggingIntegration turns every ERROR log into an issue event.
		# An IntegrityError is an unexpected constraint violation, so its
		# exception (and constraint text) goes to the log.
		if code == "conflict":
			logger.warning(
				f"{status_code} {code}: {message} "
				f"(correlation_id={request_correlation_id(request)})",
				exc_info=exc if isinstance(exc, IntegrityError) else None,
			)
		elif code in {"hardware_io", "timeout"}:
			logger.error(
				f"{status_code} {code}: {message} "
				f"(correlation_id={request_correlation_id(request)})",
				exc_info=exc,
			)
		return JSONResponse(
			status_code=status_code,
			content={"detail": error_detail(message, code)},
			headers=MAPPED_HEADERS.get(code, {}),
		)

	return handler


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
	# FastAPI's own handler answers this with a raw list of field problems;
	# the shared serializer only has a dict form, so the structured problems
	# stay in the log and the client gets the code's sentence.
	logger.warning(
		f"422 validation_failed: {exc.errors()} "
		f"(correlation_id={request_correlation_id(request)})",
	)
	return JSONResponse(
		status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
		content={"detail": error_detail("", "validation_failed")},
	)


def register_mapped_handlers(app: FastAPI) -> None:
	"""Register the standardized HTTPException body and mapped exception handlers."""
	# Register on the Starlette base class: the router raises 404/405 as base
	# instances, and the MRO lookup then also covers FastAPI-subclass raises.
	app.add_exception_handler(StarletteHTTPException, http_exception_handler)
	# FastAPI registers its own RequestValidationError handler at construction;
	# this replaces it so a failed pydantic validation becomes a standard
	# {message, code} body instead of a raw list of field problems.
	app.add_exception_handler(RequestValidationError, request_validation_handler)
	for exc_type, status_code, code in MAPPED_EXCEPTIONS:
		app.add_exception_handler(exc_type, _make_mapped_handler(status_code, code))
