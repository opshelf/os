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
import re
from typing import Any, cast

import sentry_sdk
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from openshelf_os.config.exceptions import ConfigError
from openshelf_os.exceptions import ConfigError as HardwareConfigError
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError

# Exception types that are not code bugs. In an unhandled 500 they get a
# derived code and, when listed, a message that is safe to surface; everything
# else stays generic. Full detail remains in the log and Sentry, tied to the
# client by the correlation id.
SAFE_500_TYPES: tuple[tuple[type[BaseException], str, str | None], ...] = (
	# (exception type, code, message when str(exc) is empty; None = stay generic)
	(TimeoutError, "timeout", "Operation timed out"),
	(ConfigError, "config_error", "Invalid device configuration"),
	(HardwareConfigError, "config_error", "Invalid device configuration"),
	(SQLAlchemyError, "db_error", None),
)


def internal_error_detail(exc: BaseException) -> tuple[str, str]:
	"""Return the (code, message) for an unhandled exception's 500 body."""
	for exc_type, code, fallback in SAFE_500_TYPES:
		if isinstance(exc, exc_type):
			message = str(exc) or (fallback or "Internal server error")
			return code, message
	return "internal_error", "Internal server error"


def capture_unhandled_exception(
	exc: BaseException, path: str, method: str, correlation_id: str | None
) -> None:
	"""Report an unhandled request exception to Sentry when it is initialized."""
	if sentry_sdk.is_initialized() is False:
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
	status.HTTP_409_CONFLICT: "conflict",
	status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_failed",
	status.HTTP_429_TOO_MANY_REQUESTS: "queue_full",
	status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
	status.HTTP_502_BAD_GATEWAY: "bad_gateway",
	status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
	status.HTTP_504_GATEWAY_TIMEOUT: "timeout",
}


def error_detail(message: str, code: str, **extra: Any) -> dict[str, Any]:
	"""Build the structured error body: ``{"message": ..., "code": ...}``."""
	detail: dict[str, Any] = {"message": message, "code": code}
	detail.update(extra)
	return detail


def api_error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
	"""Build an ``HTTPException`` whose detail carries message and code."""
	return HTTPException(status_code=status_code, detail=error_detail(message, code, **extra))


def standardize_http_detail(exc: HTTPException) -> Any:
	"""Wrap string details into the structured form; dict/list pass through."""
	if isinstance(exc.detail, str):
		return error_detail(exc.detail, DEFAULT_STATUS_CODES.get(exc.status_code, "error"))
	return exc.detail


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
	# Mirrors Starlette's default HTTPException handler, but standardizes
	# string details into the structured {message, code} form.
	http_exc = cast(HTTPException, exc)
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


def _clean_message(exc: BaseException) -> str:
	# SQLAlchemy DBAPIError messages carry a formatted [SQL: ...] tail. Prefer
	# the original database exception; for manually constructed errors strip the
	# "(module.Class) " prefix from the first line.
	if isinstance(exc, IntegrityError):
		orig = getattr(exc, "orig", None)
		message = str(orig) if orig is not None else ""
		if not message:
			first_line = str(exc).splitlines()[0] if str(exc) else ""
			message = re.sub(r"^\(\S+\) ", "", first_line)
		return message or "conflict"
	# OSError str() prefixes the errno, e.g. "[Errno 5] short write to printer".
	if isinstance(exc, OSError):
		message = re.sub(r"^\[Errno \d+\] ", "", str(exc))
		return message or "error"
	return str(exc) or "error"


def _make_mapped_handler(status_code: int, code: str):
	async def handler(request: Request, exc: BaseException) -> JSONResponse:
		return JSONResponse(
			status_code=status_code,
			content={"detail": error_detail(_clean_message(exc), code)},
		)

	return handler


def register_mapped_handlers(app: FastAPI) -> None:
	"""Register the standardized HTTPException body and mapped exception handlers."""
	app.add_exception_handler(HTTPException, http_exception_handler)
	for exc_type, status_code, code in MAPPED_EXCEPTIONS:
		app.add_exception_handler(exc_type, _make_mapped_handler(status_code, code))
