"""Shared API error construction and exception-to-status mapping.

Routes build HTTP errors with :func:`api_error` so every error body carries a
stable machine-readable ``code``. Exception types that represent client input
problems or known device states (rather than internal bugs) are registered on
the app with :func:`register_mapped_handlers`, so they become typed responses
without per-route ``try/except`` conversion. Starlette resolves the most
specific registered handler via the exception MRO, so subclasses keep their own
more specific mapping.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, cast

import sentry_sdk
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from openshelf_os.config.exceptions import ConfigError
from openshelf_os.exceptions import ConfigError as HardwareConfigError
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError

logger = logging.getLogger(__name__)

# Exception types that are not code bugs. In an unhandled 500 they get a
# derived code and, when listed, a message that is safe to surface; everything
# else stays generic. Full detail remains in the log and Sentry, tied to the
# client by the correlation id.
SAFE_500_TYPES: tuple[tuple[type[BaseException], str, str | None, bool], ...] = (
	# (exception type, code, fallback message, surface str(exc)?)
	# surface str=True -> str(exc), or the fallback when empty
	# surface str=False -> the fallback text, or the generic message when None
	# (str(exc) is not safe to surface: config loader
	# messages embed absolute paths; SQLAlchemy messages
	# embed the statement and bound parameters)
	(TimeoutError, "timeout", "Operation timed out", True),
	(ConfigError, "config_error", "Invalid device configuration", False),
	(HardwareConfigError, "config_error", "Invalid device configuration", False),
	(SQLAlchemyError, "db_error", None, False),
)


def internal_error_detail(exc: BaseException) -> tuple[str, str]:
	"""Return the (code, message) for an unhandled exception's 500 body."""
	for exc_type, code, fallback, surface_str in SAFE_500_TYPES:
		if isinstance(exc, exc_type):
			if surface_str:
				message = str(exc) or (fallback or "Internal server error")
			else:
				message = fallback or "Internal server error"
			return code, message
	return "internal_error", "Internal server error"


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
	"conflict": "That action does not fit what the cabinet is doing right now.",
	"config_error": "The cabinet settings are not valid.",
	"db_error": "The cabinet could not save the change. Try again.",
	"error": "The cabinet could not complete the action.",
	"forbidden": "You do not have permission to do that.",
	"hardware_io": "A part of the cabinet did not respond.",
	"internal_error": "The cabinet could not complete the action.",
	"method_not_allowed": "That request is not supported.",
	"not_found": "That entry is no longer there.",
	"queue_full": "The cabinet is busy. Wait for the current move to finish.",
	"service_unavailable": "The cabinet is not ready. Try again in a moment.",
	"timeout": "The cabinet took too long to respond. Try again.",
	"unauthorized": "Please sign in again.",
	"validation_failed": "Some details are not valid.",
}

GENERIC_ERROR_MESSAGE = CODE_MESSAGES["error"]

# Text that is never worth showing: a traceback, a formatted statement, or a
# stack frame. The server log and Sentry keep the real detail.
_TECHNICAL_MARKERS = ("Traceback (most recent call last)", "[SQL:", "\tat ", '.py", line ')

# Absolute paths on the device. Matched on a fixed list of roots so ordinary
# copy ("3/4 inch") is left alone.
_SERVER_PATH = re.compile(
	r"['\"]?/(?:dev|app|etc|var|srv|home|mnt|opt|usr|proc|sys|run|tmp|Users)(?:/[\w.@+-]+)*/?['\"]?"
)

# A leading identifier-like token, e.g. "Z_EXTENSION_PLACE not configured" or
# "shelf_idx must be non-negative". Left as is rather than sentence-cased.
_IDENTIFIER_START = re.compile(r"^(?:[A-Z_]{2,}_|[a-z][a-z0-9]*_)")


def normalize_operator_message(message: str, code: str) -> str:
	"""Return one sentence-case operator sentence for a client-visible message.

	Messages reach here from routes, mapped exceptions and 500 handlers, so the
	input ranges from hand-written copy to a driver exception. Client text is
	always a sentence; the technical detail stays in the log, tied to the client
	by the correlation id.
	"""
	fallback = CODE_MESSAGES.get(code, GENERIC_ERROR_MESSAGE)
	text = " ".join((message or "").split("\n")[0].split())
	if text.lower() in CODE_MESSAGES:
		# A route that had nothing to say sent its own code as the message.
		return CODE_MESSAGES[text.lower()]
	if text == "" or text.lower() in {"error", "exception", "failed"}:
		return fallback
	if any(marker in text for marker in _TECHNICAL_MARKERS):
		return fallback
	if _SERVER_PATH.search(text) is not None:
		text = _SERVER_PATH.sub("", text).strip(" :,-;'\"")
		if len(text) < 4 or text in {"(", "()"}:
			return fallback
	if text[0].islower() and _IDENTIFIER_START.match(text) is None:
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
		detail["message"] = normalize_operator_message(str(detail.get("message") or ""), code)
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
	# sentence and the log keeps the original exception.
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
		if code == "conflict":
			logger.warning(
				f"{status_code} {code}: {message} "
				f"(correlation_id={request_correlation_id(request)})",
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


def register_mapped_handlers(app: FastAPI) -> None:
	"""Register the standardized HTTPException body and mapped exception handlers."""
	# Register on the Starlette base class: the router raises 404/405 as base
	# instances, and the MRO lookup then also covers FastAPI-subclass raises.
	app.add_exception_handler(StarletteHTTPException, http_exception_handler)
	for exc_type, status_code, code in MAPPED_EXCEPTIONS:
		app.add_exception_handler(exc_type, _make_mapped_handler(status_code, code))
