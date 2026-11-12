"""Shared OpenAPI concepts; endpoint and field prose lives beside its implementation."""

import json
from typing import Any

from pydantic import BaseModel, Field

from openshelf_os_server.errors import CODE_MESSAGES

DESCRIPTION = """Local device API for engineers working on inventory, robot control, calibration,
and diagnostics. All application routes, including engineering and development operations,
are documented here. Cloud integrators should use the
[Cloud API](https://api.opshelf.com/docs/cloud/).

## Connect and authenticate
Base URL: `http://<device-ip>:8000`, using the device’s IP address on your network
(for example, `http://192.168.1.50:8000`). Append the documented `/api/...` path.
Log in with JSON email/PIN at
`POST /api/auth/login/json`, then send `Authorization: Bearer <access_token>`.
User tokens can also be passed as `?token=` for streaming/image clients; prefer the
header where possible. A header token takes precedence over a query token.
The scoped relay service credential is accepted only by the inventory, command,
status/event, and printing routes that declare it; it must be sent in the header.
Every operation with a security requirement needs a valid access token in production,
demo and mock modes, whatever the config file says. Only development mode answers a request
without a valid token as the developer account. Documentation describes the authenticated
contract; startup settings do not remove routes from the schema.

Access tokens expire `expires_in` seconds after login (30 minutes by default). A missing
token returns `401` with code `unauthorized`; an expired, invalid or malformed token returns
`401` with code `token_invalid`; both carry `WWW-Authenticate: Bearer`. Renew before expiry or
on that `401`: `POST /api/auth/refresh` with the refresh token returns a new access token
and the same refresh token (not rotated, valid 7 days from login by default), then retry the
request. When the refresh returns `401`, log in again. Streams (SSE, MJPEG, camera
WebSocket) check the token when they connect, so reconnect with a current token.
`/api/camera/ws/{camera_id}` is not in this schema; it closes with `4401` for a missing or
invalid token.

Role permissions are checked in addition to authentication. Item access levels can
further restrict retrieval. Public operations have no security requirement in this schema;
first-run operations additionally enforce onboarding state.

## Commands and errors
Inventory edits do not move hardware. Submit a command or bin workflow for physical
movement, save its identifier, and observe its terminal result. Acceptance is not completion.
Some engineering GET operations can start work; read their descriptions before use.
Use the physical emergency stop for immediate safety-critical stopping.

Errors return `{"detail": {"message": ..., "code": ..., "correlation_id": ...}}`, with
operation-specific fields added for some codes (for example `needs_home_axes` on
`home_required`, `active_sources` on `safety_interruption_active`). Branch on `code`, not on
the status or the message: one status covers several codes (`401` is `unauthorized`,
`token_invalid`, `login_failed` or `refresh_token_invalid`; `409` includes
`item_already_stored`, `reserved_tray_item`, `target_in_flight`, `home_required`,
`reset_required`, `tray_on_tool` and `robot_restart_required`; `404` includes
`command_not_found`; `503` includes `robot_not_initialized`). `message` is an operator
sentence whose wording can change between releases; `GET /api/system/error-messages`
lists the current sentence for every code. Request validation failures return `422` with
code `validation_failed` and no per-field list. Preserve `correlation_id` (also the
`X-Correlation-ID` response header) when reporting failures; callers can supply this header
to correlate requests. A `503` indicates an unavailable dependency, not necessarily a
stopped robot. Check state before retrying a mutation.

Lengths and coordinates are millimeters unless stated otherwise; masses are grams.
Shelf/cell indices are zero-based. Operating mode is selected at startup, not through this API.
"""

STORAGE_DESCRIPTION = """Inventory, occupancy, bin workflows, and robot grid geometry are distinct.
Item CRUD changes database records; commands move physical items. Database locations
supply occupancy for the robot status stream. Bin home locations require bin workflows,
not direct retrieval commands.
"""

TAGS = [
    {"name": name, "description": description}
    for name, description in [
        ("auth", "User login, token refresh, and session identity. Logout does not revoke tokens."),
        ("system", "Device identity, operating mode, startup policy, and process restart."),
        (
            "robot",
            "Command lifecycle, status/events, and low-level hardware diagnostics. Engineering operations can move hardware or change safety behavior.",
        ),
        ("storage", STORAGE_DESCRIPTION),
        (
            "import-jobs",
            "Relay-created inventory imports. The device owns each job; the kiosk walks it unit by unit (measure, print, scan, store) and fills trays with small units. One job runs at a time.",
        ),
        (
            "printer",
            "Barcode labels and printer diagnostics. Mock printing produces no physical label.",
        ),
        ("chemicals", "Authenticated CAS Common Chemistry search and detail lookup."),
        ("cabinet", "Physical cabinet doors and latch outputs."),
        ("camera", "Camera discovery and live MJPEG streams."),
        ("iobox-sizer", "IO-box sizing, observations, and empty-box calibration."),
        (
            "calibration",
            "Engineering calibration. Coordinates are millimeters unless stated otherwise. Motion requires available hardware and exclusive access; saves may persist configuration and move hardware. These routes are documented in every operating mode, not guaranteed available in every mode.",
        ),
        (
            "demo",
            "Authenticated demonstration-cycle control. Requires configured cells and robot hardware.",
        ),
        ("wifi", "NetworkManager wireless configuration on the device."),
        (
            "onboarding",
            "Initial network and administrator setup. Public mutations are restricted by first-run state.",
        ),
        (
            "users",
            "User administration. All routes require users:read; writes/deletes require their additional permission.",
        ),
        (
            "roles",
            "Role administration. All routes require roles:read; writes/deletes require their additional permission.",
        ),
        ("access_levels", "Access levels used to decide item retrieval permissions."),
        ("audit", "Request audit history and integrity verification. Requires audit:read."),
        ("report", "Operator-submitted diagnostics with runtime context."),
        ("telemetry", "Cumulative device counters."),
    ]
]


class ErrorResponse(BaseModel):
    """Application errors, including request validation failures (`422`)."""

    detail: str | dict[str, Any] | list[Any] = Field(
        description=(
            "Error object with `message` (operator sentence), `code` (stable identifier to branch on), "
            "`correlation_id` when known, and operation-specific fields for some codes."
        ),
        examples=[
            {
                "message": CODE_MESSAGES["token_invalid"],
                "code": "token_invalid",
                "correlation_id": "0f8e2c1a-3b4d-4e5f-8a9b-1c2d3e4f5a6b",
            }
        ],
    )


def error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    """Reusable response declarations only; does not install runtime validation."""
    descriptions = {
        400: "Invalid operation or device configuration",
        401: "`unauthorized` (no token, or inactive account) or `token_invalid` (expired, invalid or malformed token); renew the token and retry",
        403: "Permission or operating-state restriction",
        404: "Resource or configured hardware not found",
        409: "Operation conflicts with current state",
        422: "Request validation failed (`validation_failed`)",
        429: "Command queue capacity exceeded; retry after the indicated delay",
        500: "Internal failure; retain the correlation ID for diagnosis",
        502: "Invalid or failed dependency response",
        503: "Required hardware or service is unavailable",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


class BooleanSuccess(BaseModel):
    success: bool = Field(description="Operation result; inspect even when HTTP status is 200")


class MessageResponse(BaseModel):
    message: str = Field(description="Operation acknowledgment")


class HealthResponse(BaseModel):
    status: str
    mode: str
    robot_status: str
    robot_phase: str | None = None
    robot_error: str | None = None
    robot_error_type: str | None = None


class CameraHealthEntry(BaseModel):
    id: str
    online: bool
    has_frame: bool


class CameraHealthResponse(BaseModel):
    cameras: list[CameraHealthEntry]


class SystemInfoResponse(BaseModel):
    device_id: str
    device_alias: str | None
    release_sha: str | None
    release_branch: str | None
    lan_ip: str | None


def response_docs(
    model: Any = None,
    errors: tuple[int, ...] = (),
    existing: dict | None = None,
) -> dict:
    """Merge native response metadata without altering response serialization."""
    responses = error_responses(*errors)
    if model is not None:
        responses[200] = {"model": model, "description": "Operation result"}
    for code, metadata in (existing or {}).items():
        responses[code] = {**responses.get(code, {}), **metadata}
    return responses


def stream_docs(media_type: str, description: str, example: str | None = None) -> dict:
    content: dict[str, Any] = {"schema": {"type": "string", "description": description}}
    if example is not None:
        content["example"] = example
    elif media_type != "text/event-stream":
        content["schema"]["format"] = "binary"
    return {200: {"description": description, "content": {media_type: content}}}


PARAMETER_DESCRIPTIONS = {
    "barcode": "Item barcode identifying the inventory record.",
    "workflow_id": "Identifier of the bin workflow to inspect or operate on.",
    "motor_name": "Motor identifier returned by the motor diagnostics endpoint.",
    "shelf_idx": "Zero-based shelf index.",
    "cell_idx": "Zero-based cell index within the shelf.",
    "role_id": "Role identifier.",
    "user_id": "User identifier.",
    "log_id": "Audit log entry identifier.",
    "camera_id": "Camera identifier returned by camera discovery.",
    "cas_number": "CAS Registry Number to look up.",
    "command_id": "Command identifier returned when the command was accepted.",
    "offset": "Number of records to skip before returning results.",
    "skip": "Number of records to skip before returning results.",
    "limit": "Maximum number of records to return.",
    "since": "Exclusive lower bound on the event log `seq`; only events with a higher `seq` are returned.",
}


def configure_security_documentation(app: Any) -> None:
    """Describe the already-supported query-token alternative using standard security."""
    original = app.openapi

    def openapi() -> dict:
        schema = original()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes["UserBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT or scoped relay service token",
            "description": "User token from JSON login. Selected routes also accept the scoped relay service token in this header.",
        }
        schemes["UserQueryToken"] = {
            "type": "apiKey",
            "in": "query",
            "name": "token",
            "description": "User access token for clients unable to set headers. Service credentials are not accepted here.",
        }
        for path in schema["paths"].values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for parameter in operation.get("parameters", []):
                    description = PARAMETER_DESCRIPTIONS.get(parameter.get("name"))
                    if description:
                        parameter.setdefault("description", description)
                # Validation failures use the shared error body (errors.request_validation_handler),
                # not FastAPI's default field list.
                validation = operation.get("responses", {}).get("422", {})
                for media in validation.get("content", {}).values():
                    if media.get("schema") == {"$ref": "#/components/schemas/HTTPValidationError"}:
                        media["schema"] = {"$ref": "#/components/schemas/ErrorResponse"}
                        validation["description"] = "Request validation failed (`validation_failed`)"
                security = operation.get("security", [])
                if {"UserBearer": []} in security and {"UserQueryToken": []} not in security:
                    security.append({"UserQueryToken": []})
        components = schema["components"].setdefault("schemas", {})
        if '"#/components/schemas/HTTPValidationError"' not in json.dumps(schema["paths"]):
            components.pop("HTTPValidationError", None)
            if '"#/components/schemas/ValidationError"' not in json.dumps(schema):
                components.pop("ValidationError", None)
        return schema

    app.openapi = openapi
