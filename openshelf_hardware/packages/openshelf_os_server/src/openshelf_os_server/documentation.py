"""Shared OpenAPI concepts; endpoint and field prose lives beside its implementation."""

from typing import Any

from pydantic import BaseModel, Field

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
Development authentication settings may select an auto-user. Documentation describes
the authenticated contract; startup settings do not remove routes from the schema.

Role permissions are checked in addition to authentication. Item access levels can
further restrict retrieval. Public operations have no security requirement in this schema;
first-run operations additionally enforce onboarding state.

## Commands and errors
Inventory edits do not move hardware. Submit a command or bin workflow for physical
movement, save its identifier, and observe its terminal result. Acceptance is not completion.
Some engineering GET operations can start work; read their descriptions before use.
Use the physical emergency stop for immediate safety-critical stopping.

Errors use `detail`, either text, a structured object (usually `message` and `code`),
or validation details. Preserve `X-Correlation-ID` when reporting failures; callers can
supply this header to correlate requests. A `503` indicates an unavailable dependency,
not necessarily a stopped robot. Check state before retrying a mutation.

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
    """Application errors; structured detail can include operation-specific context."""

    detail: str | dict[str, Any] | list[Any] = Field(
        description="Text, an operation-specific error object, or request validation errors.",
        examples=[{"message": "Home required before robot commands.", "code": "home_required"}],
    )


def error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    """Reusable response declarations only; does not install runtime validation."""
    descriptions = {
        400: "Invalid operation or device configuration",
        401: "Authentication required or token invalid",
        403: "Permission or operating-state restriction",
        404: "Resource or configured hardware not found",
        409: "Operation conflicts with current state",
        422: "Request validation failed",
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
                security = operation.get("security", [])
                if {"UserBearer": []} in security and {"UserQueryToken": []} not in security:
                    security.append({"UserQueryToken": []})
        return schema

    app.openapi = openapi
