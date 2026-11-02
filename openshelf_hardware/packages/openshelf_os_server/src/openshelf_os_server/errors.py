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
from http import HTTPStatus
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
from openshelf_os.faults import FAULT_MESSAGES, exception_debug, fault_code
from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.core.chemicals import ChemicalNotFound, ChemicalUnavailable
from openshelf_os_server.core.command_lifecycle import (
	CommandQueueFullError,
	CommandRequestConflictError,
	CommandTargetInFlightError,
)
from openshelf_os_server.core.validation import CellOccupiedError, ValidationError
from openshelf_os_server.settings import DEVELOPER_SYSTEM_USER_EMAIL, get_settings

logger = logging.getLogger(__name__)

# Exception types that are not code bugs. In an unhandled 500 they get a
# derived code and its CODE_MESSAGES sentence; str(exc) never reaches the
# message (config loader text embeds absolute paths, SQLAlchemy text the
# statement and bound parameters). Full detail remains in the log, Sentry and
# the developer's detail.debug, tied to the client by the correlation id.
SAFE_500_TYPES: tuple[tuple[type[BaseException], str], ...] = (
	(TimeoutError, "timeout"),
	(ConfigError, "config_error"),
	(HardwareConfigError, "config_error"),
	(SQLAlchemyError, "db_error"),
)


def internal_error_detail(exc: BaseException) -> tuple[str, str]:
	"""Return the (code, message) for an unhandled exception's 500 body."""
	for exc_type, code in SAFE_500_TYPES:
		if isinstance(exc, exc_type):
			return code, CODE_MESSAGES.get(code, GENERIC_ERROR_MESSAGE)
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
	"access_level_invalid": "Choose an access level and try again.",
	"access_level_not_found": "That access level no longer exists.",
	"admin_already_created": "The administrator account already exists.",
	"admin_role_unavailable": "The robot is not ready to create the administrator yet. Try again in a moment.",
	"audit_log_not_found": "That audit entry no longer exists.",
	"bad_gateway": "The machine could not reach a part it needs. Try again in a moment.",
	"bad_request": "The machine could not do what you asked. Reload and try again.",
	"barcode_required": "Enter the item barcode.",
	"barcode_taken": "That barcode is already in use.",
	"bin_access_denied": "This bin also holds items you cannot access. Ask a supervisor to retrieve it.",
	"bin_home_missing": "The bin's home cell is missing from storage.",
	"bin_item_delete_requires_admin": "Only an admin can delete an item that is in a bin.",
	"bin_item_edit_requires_admin": "Only an admin can change the size or access level of an item in a bin.",
	"bin_not_assigned": "No bin is assigned to this cell.",
	"bin_not_empty": "Empty the bin and return it first.",
	"bin_not_found": "That barcode is not a bin.",
	"bin_workflow_conflict": "The robot cannot do that right now. Wait for it to finish and try again.",
	"bin_workflow_not_found": "This bin task no longer exists.",
	"cabinet_not_configured": "This machine cannot open its doors.",
	"cabinet_release_control_failed": "The machine did not release. Check the machine status before releasing it again.",
	"cabinet_status_failed": "The machine's status could not be read. Try again.",
	"calibration_running": "The robot is calibrating.",
	"calibration_session_required": "No calibration session is active. Move to a cell first.",
	"calibration_test_failed": "The cycle test did not finish. Check the robot before continuing.",
	"calibration_test_not_running": "No calibration test queue is running.",
	"calibration_test_running": "A calibration test queue is already running.",
	"camera_not_found": "That camera is not set up on this machine.",
	"camera_unavailable": "That camera is not delivering frames.",
	"cas_number_invalid": "That CAS number is not valid.",
	"cell_assigned": "This cell is already assigned to a bin.",
	"cell_not_calibrated": "That cell has no calibration point. Calibrate it first.",
	"cell_not_found": "That cell does not exist.",
	"cell_occupied": "This cell holds a bin or a stored item. Move or remove it first.",
	"cell_size_required": "Set the cell's size before assigning a bin.",
	"cell_status_invalid": "That cell's status is not valid.",
	"cell_too_small": "The item does not fit in that cell. Choose a larger one.",
	"cell_unavailable": "That cell is not available. Choose another one.",
	"cells_required": "Choose at least one cell to update.",
	"chemical_not_found": "That chemical is not in the database.",
	"chemical_query_too_short": "Enter at least two characters.",
	"chemical_unavailable": "Chemical lookup is not available right now. Try again in a moment.",
	"command_not_found": "That robot command does not exist.",
	"conflict": "The robot cannot do that right now. Wait for it to finish and try again.",
	"cycle_test_mode": "Cycle tests run only in development or mock mode because they create and delete items.",
	"cycle_test_not_paused": "The cycle test is not paused.",
	"cycle_test_not_running": "No cycle test is running.",
	"cycle_test_running": "A cycle test is already running.",
	"db_error": "The machine could not save the change. Try again.",
	"demo_not_configured": "The demo cell map is not set up on this machine.",
	"demo_not_running": "The demo cycle is not running.",
	"demo_running": "The robot is running the demo cycle.",
	"dev_user_missing": "The developer user is missing on this machine.",
	"development_mode_only": "That is available only in development mode.",
	"dimension_invalid": "Enter a size greater than zero.",
	"door_control_failed": "The machine's door did not move. Check that nothing blocks it before moving it again.",
	"door_positions_invalid": "Those IO door positions are not valid.",
	"email_in_use": "That email already belongs to another operator.",
	"enqueue_failed": "The robot could not queue the command. Try again.",
	"error": "The machine could not complete the action. Check the machine status before trying again.",
	"forbidden": "You do not have permission to do that.",
	"frame_encode_failed": "The IO box camera image could not be read. Try again.",
	"grid_load_failed": "The shelf layout could not be read. Try again.",
	"grid_update_failed": "The shelf layout could not be saved. Try again.",
	"hardware_io": "A part of the machine did not respond. Check the machine and try again.",
	"home_required": "The robot needs to be reset before this can continue.",
	"internal_error": "The machine could not complete the action. Check the machine status before trying again.",
	"invalid_location": "That cell is not valid.",
	"io_box_occupied": "The IO box is not empty. Take the item off the scale and wait for it to read empty.",
	"iobox_sizer_unavailable": "The IO box measure tool is not available. Try again.",
	"item_access_denied": "You do not have permission to retrieve this item.",
	"item_already_stored": "This item is already stored. Retrieve it before storing it again.",
	"item_details_required": "This item needs a name and size before it can be stored.",
	"item_not_found": "There is no item in that cell.",
	"label_print_failed": "The new label code is saved, but the printer did not print. The old label no longer works.",
	"label_scan_failed": "The label scan could not be sent. Try again.",
	"laser_calibration_failed": "The laser calibration failed. Check the robot, then run it again.",
	"laser_calibration_not_running": "No laser auto calibration is running.",
	"laser_calibration_running": "Laser auto calibration is already running.",
	"laser_read_failed": "The laser distance could not be read. Check that the laser is connected and its path is clear.",
	"location_add_failed": "The location could not be added.",
	"location_not_found": "That location does not exist.",
	"login_failed": "The sign-in details are not valid.",
	"login_rate_limited": "Too many sign-in attempts. Wait a moment and try again.",
	"method_not_allowed": "The machine could not do what you asked. Reload and try again.",
	"mock_mode_only": "That is available only in mock mode.",
	"mock_mode_unsupported": "Mock mode supports only store and retrieve.",
	"motor_alarm_clear_failed": "The robot's alarm could not be cleared. Find the cause on the drive before clearing it again.",
	"motor_home_failed": "The robot could not be reset. Check the robot status before resetting it again.",
	"motor_move_failed": "The robot could not move. Check the robot status before moving it again.",
	"motor_not_configured": "That motor is not configured on this machine.",
	"movement_in_progress": "The robot is already moving. Wait for it to finish.",
	"no_compatible_bin": "No bin can take that item right now.",
	"not_found": "The machine could not find what you asked for. Reload and try again.",
	"offsets_incomplete": "Enter the X, Y and Z offset values together.",
	"onboarding_completed": "Setup is already finished.",
	"onboarding_state_unavailable": "The robot onboarding state is not available. Try again.",
	"power_cycle_failed": "The machine could not power cycle that part. Check its power rail settings.",
	"power_rail_not_configured": "That power rail is not configured on this machine.",
	"queue_full": "The robot is busy. Wait for the current move to finish.",
	"refresh_token_invalid": "Your sign-in expired. Sign in again.",
	"relay_identity_unavailable": "The relay service identity is not available.",
	"request_invalid": "That request could not be sent. Reload and try again.",
	"request_replay": "That request was already sent. Reload and try again.",
	"reserved_bin_barcode": "That barcode belongs to a bin. Scan the item barcode instead.",
	"reserved_tray_item": "That item is already stored in a bin. Use the bin workflow instead.",
	"reset_required": "The robot needs to be reset before this can continue.",
	"robot_busy": "The robot is busy with another task.",
	"robot_disabled": "The robot is not set up to move. Check its settings.",
	"robot_not_initialized": "The robot has not started. Check the robot status.",
	"role_in_use": "This role still has operators assigned to it. Reassign them first.",
	"role_name_in_use": "That role name is already in use.",
	"role_not_found": "That role no longer exists.",
	"safety_interruption_active": "The robot is still stopped. Clear the reason and try again.",
	"scale_calibration_failed": "The scale could not be calibrated. Check that it is steady and holds only the reference weight, then calibrate again.",
	"scale_config_failed": "The scale is not available on this machine.",
	"scale_not_enabled": "The machine scale is turned off in the settings.",
	"scale_read_failed": "Could not read the scale. Try again.",
	"scale_settings_failed": "The scale settings could not be saved. Try again.",
	"scale_tare_failed": "The scale could not be zeroed. Check that it is empty and steady, then zero it again.",
	"self_delete_forbidden": "You cannot delete the account you are signed in with.",
	"service_unavailable": "The machine is not ready. Try again in a moment.",
	"shelf_not_found": "That shelf does not exist.",
	"shelf_occupied": "This shelf, or one above it, holds a bin or a stored item. Move or remove them first.",
	"target_in_flight": "The robot is already working on this item or cell. Wait for it to finish.",
	"timeout": "The robot took too long to respond. Check the robot status before trying again.",
	"token_invalid": "Your sign-in expired. Sign in again.",
	"tray_config_conflict": "The bin's settings conflict with the stored items. Check the bin's barcode and settings.",
	"tray_fetch_failed": "The bin could not be fetched. Check the robot status before fetching it again.",
	"tray_on_tool": "The gripper is still holding a tray or bin. Release it and take it off the tool.",
	"tray_return_failed": "The bin could not be returned. Check the robot status before returning it again.",
	"unauthorized": "Your sign-in expired. Sign in again.",
	"unknown_item": "That item could not be found.",
	"user_not_found": "That operator account no longer exists.",
	"validation_failed": "Check the details you entered and try again.",
	"wifi_connect_failed": "The machine could not connect to that network. Try again.",
	"wifi_connect_failed_offline": "The machine could not connect and may now be offline.",
	"wifi_list_failed": "The network list could not be read. Try again.",
	"wifi_network_required": "Choose a network to connect to.",
	"wifi_unavailable": "Network settings are not available on this machine.",
	# Robot fault codes, shared with the worker's failure events.
	**FAULT_MESSAGES,
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


def _raised_from(exc: BaseException) -> BaseException | None:
	"""The failure a route converted into an HTTP error, `from exc` or not."""
	if exc.__cause__ is not None:
		return exc.__cause__
	return None if exc.__suppress_context__ else exc.__context__


def _request_token(request: Request) -> str | None:
	scheme, _, token = request.headers.get("Authorization", "").partition(" ")
	if scheme.lower() == "bearer" and token != "":
		return token
	return request.query_params.get("token") or None


def debug_allowed(request: Request) -> bool:
	"""True when the caller is the developer system user the dev UI signs in as.

	Only those clients get ``detail.debug``: exception text can carry paths, ids
	and driver output that the operator copy rule keeps away from the kiosk.
	"""
	auth = get_settings().auth
	token = _request_token(request)
	if token is None:
		return (
			(auth.enabled is False or auth.require_auth is False)
			and auth.dev_mode is True
			and auth.dev_auto_user == DEVELOPER_SYSTEM_USER_EMAIL
		)
	payload = AuthService(auth.jwt_secret or "", auth.jwt_algorithm).decode_token(token)
	return payload is not None and payload.get("type") == "access" and payload.get("email") == DEVELOPER_SYSTEM_USER_EMAIL


# Exceptions whose text is empty or driver output, by the code that describes them.
_FAILURE_TYPE_CODES: tuple[tuple[type[BaseException], str], ...] = (
	(asyncio.QueueFull, "queue_full"),
	(CommandQueueFullError, "queue_full"),
	(CommandTargetInFlightError, "target_in_flight"),
	(CommandRequestConflictError, "request_replay"),
	(TimeoutError, "timeout"),
	(OSError, "hardware_io"),
)


def failure_detail(exc: BaseException, fallback_code: str) -> tuple[str, str]:
	"""(code, operator sentence) for a failure recorded outside an HTTP body: a workflow
	error, a command result. The exception itself belongs in the log and `debug`."""
	code = fault_code(exc) or next(
		(code for exc_type, code in _FAILURE_TYPE_CODES if isinstance(exc, exc_type)), fallback_code
	)
	return code, CODE_MESSAGES[code]


def public_payload(payload: Any, show_debug: bool) -> Any:
	"""A robot event or command result as a client may see it: the worker attaches the
	exception behind a failure as `debug`, which only the developer user receives."""
	if show_debug is True or isinstance(payload, dict) is False or "debug" not in payload:
		return payload
	return {key: value for key, value in payload.items() if key != "debug"}


def client_detail(request: Request, detail: dict[str, Any], exc: BaseException | None) -> dict[str, Any]:
	"""Add the correlation id, and for developer clients the exception chain, to a body."""
	correlation_id = request_correlation_id(request)
	if correlation_id:
		detail["correlation_id"] = correlation_id
	if exc is not None and debug_allowed(request):
		detail["debug"] = exception_debug(exc)
	return detail


# Status per robot fault code when a route surfaces one; anything else is a 503.
_FAULT_STATUS: dict[str, int] = {
	"motor_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
	"rotation_blocked": status.HTTP_409_CONFLICT,
	"grip_dropped": status.HTTP_409_CONFLICT,
	"grip_io_box_failed": status.HTTP_409_CONFLICT,
	"grip_pick_failed": status.HTTP_409_CONFLICT,
	"grip_release_failed": status.HTTP_409_CONFLICT,
}


def classify_hardware_error(exc: BaseException) -> tuple[int, str] | None:
	"""Return a (status, code) more specific than a route's catch-all, if the chain has one."""
	code = fault_code(exc)
	if code is None:
		return None
	return _FAULT_STATUS.get(code, status.HTTP_503_SERVICE_UNAVAILABLE), code


def hardware_api_error(exc: BaseException, status_code: int, code: str, message: str) -> HTTPException:
	"""``api_error`` for a failed hardware action, under the most specific code the
	exception chain supports; the route's own code and copy are the fallback."""
	classified = classify_hardware_error(exc)
	if classified is None:
		return api_error(status_code, code, message)
	return api_error(classified[0], classified[1], CODE_MESSAGES[classified[1]])


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
		# Starlette's own 404/405 carry the bare reason phrase ("Not Found"), not copy.
		is_reason_phrase = exc.detail == HTTPStatus(exc.status_code).phrase
		return error_detail("" if is_reason_phrase else exc.detail, DEFAULT_STATUS_CODES.get(exc.status_code, "error"))
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
	detail = standardize_http_detail(http_exc)
	if isinstance(detail, dict):
		# Routes raise these `from` the real failure, so the chain is the developer detail.
		detail = client_detail(request, detail, _raised_from(http_exc))
	return JSONResponse(
		content={"detail": detail},
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
	# Driver and timeout text ("[Errno 5] short write to printer: '/dev/usb/lp0'",
	# "Gantry wait exceeded 30s timeout") is not operator copy; it reaches the log and
	# the developer's detail.debug. TimeoutError is an OSError, so it is checked first.
	if isinstance(exc, TimeoutError):
		return CODE_MESSAGES["timeout"]
	if isinstance(exc, OSError):
		return CODE_MESSAGES["hardware_io"]
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
			content={"detail": client_detail(request, error_detail(message, code), exc)},
			headers=MAPPED_HEADERS.get(code, {}),
		)

	return handler


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
	# FastAPI's own handler answers this with a raw list of field problems;
	# the shared serializer only has a dict form, so the structured problems
	# stay in the log and the client gets the code's sentence. Developer clients
	# also get the field paths, never the submitted values (a PIN can be one).
	logger.warning(
		f"422 validation_failed: {exc.errors()} "
		f"(correlation_id={request_correlation_id(request)})",
	)
	detail = client_detail(request, error_detail("", "validation_failed"), None)
	if debug_allowed(request):
		detail["debug"] = {
			"fields": [
				f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
				for error in exc.errors()
			]
		}
	return JSONResponse(
		status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
		content={"detail": detail},
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
