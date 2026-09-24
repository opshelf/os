from __future__ import annotations
from openshelf_os_server.documentation import BooleanSuccess, response_docs, stream_docs
from fastapi.responses import Response
from openshelf_os_server.core.mass import MASS_FIELDS, apply_mass, validate_mass
from openshelf_os_server.core.validation import ItemValidator, ValidationError

import asyncio
from copy import deepcopy
import json
import math
import re
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Iterable, List, Literal, NoReturn, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from openshelf_os.common import ConfigError
from openshelf_os.config.settings import BARCODE_MAX_LENGTH
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.storage import CellItem, CellStatus, Dimensions, StorageModuleCellLocation
from openshelf_os.robot.commands import (
    HomeCommand,
    InitializeCommand,
    RetrieveItemCommand,
    RobotCommands,
    StopCommand,
    StoreItemCommand,
)
import logging
from openshelf_os.robot.core import POWER_CYCLE_HOLD_S, POWER_RAILS
from openshelf_os.robot.worker import RobotWorker
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from openshelf_os_server.core.robot import get_robot_instance, get_robot_worker
from openshelf_os_server.core.robot_events import (
    EVENT_FORWARD_WAIT_TIMEOUT_S,
    event_processing_tracker,
    process_robot_event,
)
from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os_server.core.iobox_sizer import capture_store_observer_measurement
from openshelf_os_server.core.logging_config import correlation_id_var
from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.manual_motion import (
    get_manual_motion_operation,
    manual_motion_guard,
    robot_command_enqueue_guard,
)
from openshelf_os_server.core.mock_robot import (
    MOCK_CELLS_PER_SHELF,
    MOCK_SHELVES_PER_SIDE,
    MOCK_SIDES,
    MockQueueFullError,
    MockRequestConflictError,
    get_mock_robot_runtime,
    mock_cell_fits,
)
from openshelf_os_server.core.command_lifecycle import (
    CommandQueueFullError,
    CommandRequestConflictError,
    admit as admit_robot_command,
    enqueue_persisted,
    get_command as get_lifecycle_command,
    replay as replay_robot_command,
    serialize_command,
)
from openshelf_os_server.core import occupancy_cache
from openshelf_os_server.core.bin_storage import (
    DirectStoreReservationError,
    is_reserved_bin_location,
    validate_direct_store_reservation,
)
from openshelf_os_server.routers.common import SuccessResponse
from openshelf_os_server.errors import error_detail
from openshelf_os_server.settings import get_settings, get_operating_mode
from openshelf_os_server.database.session import get_db
from openshelf_os_server.database.models import AccessLevel, Item, ItemLocation, StorageBinItem, User
from .auth_deps import (
    get_current_user,
    get_relay_or_current_user,
    require_permissions,
    can_user_retrieve_item,
)

from openshelf_os.robot.status import SafetyStatus

class ScannerStatusResponse(BaseModel):
    connected: bool
    enabled: bool


class ScaleReadingResponse(BaseModel):
    weight_grams: float = Field(description="Live gross scale reading in grams")
    decimal_position: int
    status: dict[str, bool | int]
    raw_registers: dict[str, int]


class ScaleCalibrationStatusResponse(BaseModel):
    zero_ok: bool
    weight_ok: bool
    raw: int


class ScaleCalibrationResult(BaseModel):
    success: bool
    error: str | None = None
    status: ScaleCalibrationStatusResponse | None = None


class ScaleCalibrationParameters(BaseModel):
    full_scale: int = Field(description="Scale range in grams")
    calibration_weight: int = Field(description="Known calibration weight in grams")
    precision: int
    division: int


class MotorOperationResponse(BaseModel):
    success: bool
    motor: str
    position_mm: float | None = Field(default=None, description="Requested target, when this is a move")


class DoorOperationResponse(BaseModel):
    success: bool
    action: Literal["open", "close", "home"]


class PowerRailStatus(BaseModel):
    rail: str = Field(description="GPIO output identifier of the switched rail")
    supplies: str = Field(description="Hardware that loses power when this rail is cut")
    axes: list[str] = Field(
        description="Motors that lose their encoder reference when this rail is cut"
    )
    gripper: bool = Field(description="Whether the gripper servo drops with this rail")
    powered: bool | None = Field(description="Current rail state; null when it cannot be read")


class PowerRailsResponse(BaseModel):
    hold_seconds: float = Field(description="How long a cycle cuts the rail before restoring power")
    rails: list[PowerRailStatus]


class PowerCycleResponse(BaseModel):
    success: bool
    rail: str
    axes_needing_home: list[str] = Field(
        description="Axes that must be re-homed before further motion"
    )


class InitComponentResponse(BaseModel):
    component: str
    phase: str
    detail: str
    reason: str | None
    updated_at: float


class MotorsResponse(BaseModel):
    io: list[dict[str, Any]] = Field(description="Configured IO motors and their diagnostic identifiers")
    tool: list[dict[str, Any]] = Field(description="Configured tool motors and their diagnostic identifiers")
    init_phase: str
    init_components: list[InitComponentResponse]


router = APIRouter(tags=["robot"])

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)
_SCALE_NOT_ENABLED_MESSAGE = "Scale is not enabled"




def _is_scale_not_enabled(exc: Exception) -> bool:
    return str(exc) == _SCALE_NOT_ENABLED_MESSAGE


def _scale_not_enabled_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_detail(_SCALE_NOT_ENABLED_MESSAGE, "scale_not_enabled"),
    )


def _raise_scale_config_error(exc: ConfigError) -> NoReturn:
    if _is_scale_not_enabled(exc):
        raise _scale_not_enabled_exception() from exc

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc) or "Scale is not enabled or configured",
    ) from exc


async def _ensure_laser_auto_calibration_not_running() -> None:
    if await is_laser_auto_calibration_running() is False:
        return

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="laser auto calibration is running",
    )


def _reason_matches_motor(reason: Optional[str], motor_name: str, motor) -> bool:
    if not reason:
        return False

    reason_lower = reason.lower()
    if motor_name.lower() in reason_lower:
        return True

    match = _MOTOR_ID_RE.search(reason)
    if not match:
        return False

    try:
        motor_id = int(match.group(1))
    except ValueError:
        return False

    return getattr(motor, "_motor_id", None) == motor_id


def _clear_matching_motor_crash_state(worker: RobotWorker, motor_name: str, motor) -> None:
    if getattr(worker, "_is_crashed", False) is False:
        return

    if _reason_matches_motor(getattr(worker, "_crash_reason", None), motor_name, motor) is False:
        return

    worker.clear_crash_state()


def _probe_busy_reason(worker: RobotWorker) -> Optional[str]:
    manual_operation = get_manual_motion_operation()
    if manual_operation is not None:
        return manual_operation

    worker_state = getattr(worker, "__dict__", {})
    if worker_state.get("_current_command") is not None:
        return "robot_command_in_progress"

    command_queue = worker_state.get("_command_queue")
    qsize = getattr(command_queue, "qsize", None)
    if callable(qsize) is True:
        try:
            qsize_fn = cast(Callable[[], int], qsize)
            if qsize_fn() > 0:
                return "robot_command_queued"
        except Exception:
            pass

    if worker_state.get("_demo_cycling", False) is True:
        return "demo_cycle_in_progress"

    return None


def overlay_db_occupancy(payload_json: str, occupied: set[tuple[str, int, int]]) -> str:
    payload = json.loads(payload_json)
    storage = payload.get("robot_status", {}).get("storage", {})

    for side in ("left_module", "right_module"):
        for shelf_position, shelf in enumerate(storage.get(side, [])):
            shelf_idx_raw = shelf.get("shelf_idx", shelf_position)

            try:
                shelf_idx = int(shelf_idx_raw)
            except (TypeError, ValueError):
                continue

            for cell_position, cell in enumerate(shelf.get("cells", [])):
                cell_idx_raw = cell.get("cell_idx", cell_position)

                try:
                    cell_idx = int(cell_idx_raw)
                except (TypeError, ValueError):
                    continue

                key = (side, shelf_idx, cell_idx)
                if key in occupied:
                    cell["status"] = "occupied"
                elif cell.get("status") == "occupied":
                    cell["status"] = "available"

    return json.dumps(payload)


def inject_mode_field(payload_json: str) -> str:
    payload = json.loads(payload_json)
    payload["mode"] = get_operating_mode().value
    return json.dumps(payload)


class LabelScanRequest(BaseModel):
    barcode: str = Field(..., min_length=1)


class ScaleCalibrationParamsRequest(BaseModel):
    full_scale: int = Field(..., description="Full scale value in grams (e.g., 6000 for 6kg)")
    calibration_weight: int = Field(..., description="Known calibration weight in grams")
    precision: int = Field(default=1, ge=0, le=3, description="Decimal precision (0-3)")
    division: int = Field(default=1, ge=1, description="Division value")


class MotorProbeResult(BaseModel):
    motor_name: str
    responsive: bool
    position_mm: Optional[float] = None
    error: Optional[str] = None


class MotorProbeResponse(BaseModel):
    results: List[MotorProbeResult] = Field(default_factory=list)
    busy: bool = False
    reason: Optional[str] = None


class MotorMoveRequest(BaseModel):
    position_mm: float = Field(..., description="Target position in millimeters")


class SafetyGuardOverrideRequest(BaseModel):
    bypass: bool


class SafetyComponentOverrideRequest(BaseModel):
    component: Literal["light_curtain", "cabinet_doors"]
    bypass: bool


class CommandAcceptedResponse(BaseModel):
    message: Literal["success"]
    command_id: str
    status: Literal["queued", "running", "succeeded", "failed"]


class CommandLifecycleResponse(BaseModel):
    command_id: str
    request_id: Optional[str]
    command: str
    status: Literal["queued", "running", "succeeded", "failed"]
    result: Optional[dict[str, Any]]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


async def check_robot_enabled() -> None:
    settings = get_settings()
    if not settings.robot.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not enabled or configured",
        )


async def get_robot_worker_or_error() -> RobotWorker:
    try:
        return await get_robot_worker()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not initialized or failed to start",
        ) from e


async def get_command_worker_for_mode() -> RobotWorker | None:
    if get_operating_mode() == OperatingMode.mock:
        return None
    await check_robot_enabled()
    return await get_robot_worker_or_error()


async def get_optional_command_worker_for_mode() -> RobotWorker | None:
    """Resolve the worker without preempting durable idempotency replays."""
    if get_operating_mode() == OperatingMode.mock:
        return None
    if get_settings().robot.enabled is False:
        return None
    try:
        return await get_robot_worker()
    except ValueError:
        return None


def _ensure_command_allowed_while_needs_home(command: RobotCommands, worker: RobotWorker) -> None:
    if worker.needs_home() is False:
        return

    if isinstance(command, (HomeCommand, InitializeCommand, StopCommand)):
        return

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "Home required before robot commands.",
            "code": "home_required",
            "needs_home": True,
            "needs_home_axes": worker.needs_home_axes(),
            "reason": worker.needs_home_reason(),
        },
    )


async def _validate_mock_store(command: StoreItemCommand, db: AsyncSession) -> None:
    item_data = command.item or {}
    barcode = item_data.get("barcode")
    if not isinstance(barcode, str) or barcode.strip() == "" or len(barcode) > BARCODE_MAX_LENGTH:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="item.barcode is required")

    _parse_requested_store_location(item_data)
    await _validate_direct_store_reservation(item_data, db)

    item = (await db.execute(select(Item).where(Item.barcode == barcode))).scalar_one_or_none()
    if item is not None:
        bin_membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one_or_none()
        if bin_membership is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Item is already stored in a bin; retrieve it before storing again",
            )
        existing = (
            await db.execute(select(ItemLocation).where(ItemLocation.item_id == item.id))
        ).scalars().first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Item is already stored; retrieve it before storing again",
            )
        await _validate_mock_store_location(item_data, item.dimensions, db)
        return

    name = item_data.get("name")
    dimensions = item_data.get("dimensions")
    if not isinstance(name, str) or name.strip() == "" or not isinstance(dimensions, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A new item requires barcode, name, and complete dimensions",
        )
    try:
        if any(
            isinstance(dimensions.get(key), bool)
            or not isinstance(dimensions.get(key), (int, float))
            or not 0 < dimensions[key] < math.inf
            for key in ("width", "height", "length")
        ):
            raise ValueError
        if item_data.get("weight") is not None and float(item_data["weight"]) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Item dimensions and weight must be positive numbers",
        ) from exc

    await _validate_mock_store_location(item_data, dimensions, db)

    try:
        validate_mass(item_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    access_level_id = item_data.get("access_level_id")
    if access_level_id is not None and await db.get(AccessLevel, access_level_id) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid access level")


async def _validate_direct_store_reservation(item_data: dict[str, Any], db: AsyncSession) -> None:
    try:
        await validate_direct_store_reservation(item_data, db)
    except DirectStoreReservationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(str(exc), exc.code),
        ) from exc


def _fold_store_location_into_item(command: StoreItemCommand) -> StoreItemCommand:
    """Keep one internal representation of the requested destination cell.

    Public requests use a top-level `location`; the robot worker, the bin workflow, and
    admission validation read `item.location`. Folding here also keeps the durable
    idempotency payload canonical.
    """
    location = command.location
    if location is None:
        return command

    item = dict(command.item or {})
    item["location"] = dict(location)
    return StoreItemCommand(request_id=command.request_id, item=item)


def _parse_requested_store_location(item_data: dict[str, Any]) -> StorageModuleCellLocation | None:
    raw_location = item_data.get("location")
    if raw_location is None:
        return None
    try:
        if (
            not isinstance(raw_location, dict)
            or not isinstance(raw_location.get("side"), str)
            or any(
                not isinstance(raw_location.get(key), int)
                or isinstance(raw_location.get(key), bool)
                for key in ("shelf_idx", "cell_idx")
            )
        ):
            raise ValueError
        return StorageModuleCellLocation.model_validate(raw_location)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error_detail("location requires side, shelf_idx and cell_idx", "invalid_location"),
        ) from None


def _validate_requested_store_location(item_data: dict[str, Any], worker: Any) -> None:
    location = _parse_requested_store_location(item_data)
    if location is None:
        return

    robot = getattr(worker, "_robot", None)
    grid = getattr(robot, "_grid", None)
    if getattr(robot, "_demo_cell_map", None) is not None:
        # Demo execution uses its barcode mapping instead of ordinary requested cells.
        return
    if grid is None:
        # Lightweight worker doubles expose no grid; execution re-validates.
        return

    cell = grid.get_cell_if_present(location)
    if cell is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error_detail("Requested storage cell does not exist", "cell_not_found"),
        )
    if cell.status != CellStatus.AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(f"Requested storage cell is {cell.status.value}", "cell_unavailable"),
        )

    dimensions = item_data.get("dimensions")
    if not isinstance(dimensions, dict):
        return
    try:
        item = CellItem(
            dimensions=Dimensions(
                width=float(dimensions["width"]),
                height=float(dimensions["height"]),
                length=float(dimensions["length"]),
            )
        )
    except (KeyError, TypeError, ValueError):
        return
    if grid.cell_fits_item(cell, item) is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("Item does not fit the requested storage cell", "cell_too_small"),
        )


async def _validate_mock_store_location(
    item_data: dict[str, Any],
    dimensions: Any,
    db: AsyncSession,
) -> None:
    location = _parse_requested_store_location(item_data)
    if location is None:
        return

    side = location.side.value
    shelf_idx = location.shelf_idx
    cell_idx = location.cell_idx
    if (
        side not in MOCK_SIDES
        or not 0 <= shelf_idx < MOCK_SHELVES_PER_SIDE
        or not 0 <= cell_idx < MOCK_CELLS_PER_SHELF
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error_detail("Requested storage cell does not exist", "cell_not_found"),
        )

    occupied = (
        await db.execute(
            select(ItemLocation.id).where(
                ItemLocation.side == side,
                ItemLocation.shelf_idx == shelf_idx,
                ItemLocation.cell_idx == cell_idx,
            )
        )
    ).scalar_one_or_none()
    if occupied is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("Requested storage cell is occupied", "cell_unavailable"),
        )

    # A live bin home is unavailable for explicit stores, matching the maintenance status
    # production applies to reserved cells; automatic allocation avoids it instead.
    if await is_reserved_bin_location((side, shelf_idx, cell_idx), db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("Requested storage cell is reserved", "cell_unavailable"),
        )

    if isinstance(dimensions, dict) and mock_cell_fits(dimensions) is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("Item does not fit the requested storage cell", "cell_too_small"),
        )


async def _validate_mock_retrieve(
    command: RetrieveItemCommand,
    current_user: User,
    db: AsyncSession,
) -> None:
    location = command.location or {}
    side = location.get("side")
    shelf_idx = location.get("shelf_idx")
    cell_idx = location.get("cell_idx")
    if (
        side not in MOCK_SIDES
        or not isinstance(shelf_idx, int)
        or isinstance(shelf_idx, bool)
        or not isinstance(cell_idx, int)
        or isinstance(cell_idx, bool)
        or not 0 <= shelf_idx < MOCK_SHELVES_PER_SIDE
        or not 0 <= cell_idx < MOCK_CELLS_PER_SHELF
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid location: side, shelf_idx and cell_idx are required",
        )

    item = (
        await db.execute(
            select(Item)
            .join(ItemLocation)
            .where(
                ItemLocation.side == side,
                ItemLocation.shelf_idx == shelf_idx,
                ItemLocation.cell_idx == cell_idx,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No item found at location (side: {side}, shelf: {shelf_idx}, cell: {cell_idx})",
        )
    if can_user_retrieve_item(current_user, item) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to retrieve this item",
        )


@router.post(
    "/commands",
    dependencies=[Depends(get_relay_or_current_user)],
    response_model=CommandAcceptedResponse,
    responses=response_docs(
        errors=(400, 401, 403, 404, 409, 422, 429, 503),
        existing={
            409: {"description": "Idempotency conflict or command precondition failed"},
            429: {
                "description": "The active command queue is full",
                "headers": {"Retry-After": {"schema": {"type": "string"}}},
            },
        },
    ),
    summary="Queue a robot command",
)
async def robot_commands(
    command: RobotCommands,
    worker: RobotWorker | None = Depends(get_optional_command_worker_for_mode),
    current_user: User = Depends(get_relay_or_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommandAcceptedResponse:
    """Queue a robot command.

    Returns a durable `command_id` and current state after admission, not after physical
    completion. Poll `/api/robot/commands/{command_id}` for the result or subscribe to
    `/api/robot/events` before submitting. The status stream is not a command receipt.

    For store/retrieve commands, reuse the same `request_id` and payload after an uncertain
    submission. Identical replays return the existing record; a different payload returns
    `409`. Records and idempotency are retained for seven days. A terminal failure requires
    a new ID for a new attempt. Interrupted commands are failed at startup rather than
    replayed.

    Create inventory first, then submit
    `{"command":"store_item","request_id":"store-001","item":{"barcode":"880000000001"}}`. New
    command-enrolled items require positive complete dimensions; include a name for
    compatibility with mock mode. Existing items use stored dimensions and mass: update
    inventory before submitting if these must change. Accepted enrollment survives execution
    failure.

    To choose the destination cell, add a top-level `location`
    (`{"side":"left_module","shelf_idx":0,"cell_idx":0}`). The cell must exist in the
    storage grid, be available, and fit the item; a live bin home stays reserved and is
    reported unavailable. Omitting `location` keeps automatic allocation, and demo mode
    keeps its configured cell mapping.

    For retrieval, read the item first and supply its `location` (`side`, zero-based
    `shelf_idx`, `cell_idx`). Bin-managed items require the bin workflow. Clear the IO box
    before retrieving. Home when `home_required` is reported. An item reserved by a tray interaction returns
    `409`; a full queue returns `429` with `Retry-After`.

    Mock mode supports only store/retrieve, with one FIFO worker and simulated completion.
    Hardware modes also accept the engineering commands in the schema. A queued stop is not
    an emergency stop; use the physical emergency stop for immediate safety-critical
    stopping.
    """
    operating_mode = get_operating_mode()
    if isinstance(command, StoreItemCommand):
        command = _fold_store_location_into_item(command)
    submission_payload = command.model_dump(mode="json", exclude_none=True)
    request_id = getattr(command, "request_id", None)
    if operating_mode == OperatingMode.mock:
        if not isinstance(command, (StoreItemCommand, RetrieveItemCommand)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mock mode supports only store_item and retrieve_item",
            )
        runtime = get_mock_robot_runtime()
        payload = submission_payload
        try:
            replay = await runtime.replay(payload, request_id)
        except MockRequestConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request_id was already used with a different payload",
            ) from exc
        if replay is not None:
            return CommandAcceptedResponse.model_validate(
                {"message": "success", "command_id": replay.command_id, "status": replay.status}
            )

        async with robot_command_enqueue_guard():
            if isinstance(command, StoreItemCommand):
                await _validate_mock_store(command, db)
            else:
                await _validate_mock_retrieve(command, current_user, db)

            try:
                row, _ = await runtime.enqueue(
                    payload,
                    request_id=request_id,
                    correlation_id=correlation_id_var.get(),
                )
            except MockRequestConflictError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="request_id was already used with a different payload",
                ) from exc
            except MockQueueFullError as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Mock robot command queue is full",
                    headers={"Retry-After": "1"},
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return CommandAcceptedResponse.model_validate(
            {"message": "success", "command_id": row.command_id, "status": row.status}
        )

    try:
        replay = await replay_robot_command(submission_payload, request_id)
    except CommandRequestConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="request_id was already used with a different payload",
        ) from exc
    if replay is not None:
        return CommandAcceptedResponse(
            message="success",
            command_id=replay.command_id,
            status=cast(Any, replay.status),
        )

    await _ensure_laser_auto_calibration_not_running()
    if worker is None:
        await check_robot_enabled()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not initialized or failed to start",
        )
    _ensure_command_allowed_while_needs_home(command, worker)

    enrollment_item: Item | None = None
    if isinstance(command, StoreItemCommand):
        logging.info("Processing StoreItemCommand: item=%s", command.item)
        item_data = deepcopy(command.item or {})
        barcode = item_data.get("barcode")
        if any(key in item_data for key in MASS_FIELDS) and (
            not isinstance(barcode, str) or not barcode.strip() or len(barcode) > BARCODE_MAX_LENGTH
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="item.barcode is required for container mass enrollment",
            )
        _parse_requested_store_location(item_data)
        await _validate_direct_store_reservation(item_data, db)
        human_dimensions_source = "command_payload" if item_data.get("dimensions") is not None else "none"

        if barcode is not None:
            logging.info("Looking up item by barcode: %s", barcode)
            result = await db.execute(select(Item).where(Item.barcode == barcode))
            item = result.scalar_one_or_none()

            if item is not None:
                logging.info("Found item in db: name=%s, dimensions=%s", item.name, item.dimensions)
                item_data["dimensions"] = item.dimensions
                item_data["name"] = item.name
                human_dimensions_source = "db"
                item_data.pop("net_weight", None)
                if item.weight is not None:
                    item_data["weight"] = item.weight
                else:
                    item_data.pop("weight", None)

                location_result = await db.execute(
                    select(ItemLocation).where(ItemLocation.item_id == item.id)
                )
                existing_location = location_result.scalars().first()
                if existing_location is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Item is already stored; retrieve it before storing again",
                    )
                bin_membership = (
                    await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
                ).scalar_one_or_none()
                if bin_membership is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Item is stored in a bin; use the storage bin workflow",
                    )
                item_data["tare_weight"] = item.tare_weight
                item_data["full_net_weight"] = item.full_net_weight
                command = StoreItemCommand(item=item_data, request_id=request_id)
            else:
                logging.info("Item not found in db for barcode: %s", barcode)
                dimensions = item_data.get("dimensions")
                if not isinstance(dimensions, dict) or any(
                    isinstance(dimensions.get(key), bool)
                    or not isinstance(dimensions.get(key), (int, float))
                    or not 0 < dimensions[key] < math.inf
                    for key in ("width", "height", "length")
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="New items require finite positive width, height, and length",
                    )
                try:
                    ItemValidator.validate_weight(item_data.get("weight"))
                    validate_mass(item_data)
                except ValidationError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                # Prepare enrollment for the command admission transaction, so
                # rejected submissions cannot establish an operator baseline.
                item = Item(barcode=str(barcode), name=str(item_data.get("name") or barcode),
                            dimensions=dimensions,
                            weight=item_data.get("weight"), item_metadata=item_data.get("metadata") or {},
                            access_level_id=item_data.get("access_level_id"))
                apply_mass(item, item_data, enrollment=True)
                enrollment_item = item

        logging.info("Final StoreItemCommand: item=%s", command.item)
        final_item_data = command.item or {}
        _validate_requested_store_location(final_item_data, worker)
        try:
            await capture_store_observer_measurement(
                barcode=final_item_data.get("barcode"),
                item_name=final_item_data.get("name"),
                user_id=current_user.id,
                request_correlation_id=correlation_id_var.get(),
                command_payload=command.model_dump(mode="json"),
                human_dimensions=deepcopy(final_item_data.get("dimensions")),
                human_dimensions_source=human_dimensions_source,
            )
        except Exception as exc:
            logging.warning("IO box sizer observer capture failed: %s", exc)

    if isinstance(command, RetrieveItemCommand):
        if command.barcode:
            binned_item = (
                await db.execute(
                    select(Item)
                    .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                    .where(Item.barcode == command.barcode)
                )
            ).scalar_one_or_none()
            if binned_item is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Item is stored in a bin; use the storage bin workflow",
                )
        if worker.get_safety_status().get("bypassed") is not True:
            try:
                scale_status = worker.get_cached_scale_status()
                if scale_status is not None:
                    if scale_status.occupied is True:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Please remove the item from the IO box before retrieving another",
                        )
                else:
                    scale_data = await worker.get_scale_data()
                    weight = abs(scale_data["weight"])
                    if weight > 5.0:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Please remove the item from the IO box before retrieving another",
                        )
            except HTTPException:
                raise
            except Exception as exc:
                logging.warning("Scale read failed during retrieve check: %s", exc)

        location_data = command.location or {}
        side = location_data.get("side")
        shelf_idx = location_data.get("shelf_idx")
        cell_idx = location_data.get("cell_idx")

        if side is None or shelf_idx is None or cell_idx is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid location: side, shelf_idx and cell_idx are required",
            )

        result = await db.execute(
            select(Item)
            .join(ItemLocation)
            .where(
                ItemLocation.side == side,
                ItemLocation.shelf_idx == shelf_idx,
                ItemLocation.cell_idx == cell_idx,
            )
        )
        item = result.scalar_one_or_none()

        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No item found at location (side: {side}, shelf: {shelf_idx}, cell: {cell_idx})",
            )

        if can_user_retrieve_item(current_user, item) is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to retrieve this item"
            )

    async with robot_command_enqueue_guard():
        if isinstance(command, StoreItemCommand):
            await _validate_direct_store_reservation(command.item or {}, db)
        try:
            row, was_replay = await admit_robot_command(
                submission_payload,
                request_id=request_id,
                correlation_id=correlation_id_var.get(),
                enrollment_item=enrollment_item,
            )
        except CommandRequestConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request_id was already used with a different payload",
            ) from exc
        except CommandQueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Robot command queue is full",
                headers={"Retry-After": "1"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if was_replay is False:
            await enqueue_persisted(worker, command, row)
    return CommandAcceptedResponse(
        message="success",
        command_id=row.command_id,
        status=cast(Any, row.status),
    )


@router.get(
    "/commands/{command_id}",
    dependencies=[Depends(get_relay_or_current_user)],
    response_model=CommandLifecycleResponse,
    responses=response_docs(
        errors=(401, 404, 503), existing={404: {"description": "Robot command not found"}}
    ),
    summary="Read a durable command result",
)
async def get_robot_command(command_id: str) -> CommandLifecycleResponse:
    """Read a durable command result.

    Available for mock and hardware command records. `result` is null before a terminal
    result is persisted and may contain a command-specific success or failure payload
    afterward. Unknown or expired IDs return `404`. For measurement semantics, see the
    storage group; a successful retrieval does not guarantee a weight reading.
    """
    row = await get_lifecycle_command(command_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Robot command not found")
    return CommandLifecycleResponse.model_validate(serialize_command(row))


@router.post(
    "/start",
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("robot:control")),
        Depends(check_robot_enabled),
    ],
    summary="Start the robot worker",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def start_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    """Start the robot worker.

    Starts worker processing; requires `robot:control`. This is a worker lifecycle
    operation, not command submission. Returns `409` while laser auto-calibration is active
    and `503` if the robot is unavailable.
    """
    await _ensure_laser_auto_calibration_not_running()
    await worker.start()
    return SuccessResponse()


@router.post(
    "/stop",
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("robot:control")),
        Depends(check_robot_enabled),
    ],
    summary="Shut down the robot worker",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def stop_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    """Shut down the robot worker.

    Waits for worker shutdown; requires `robot:control`. This differs from submitting a
    queued `stop` command and is not a physical emergency stop. Unavailable during laser
    auto-calibration.
    """
    await _ensure_laser_auto_calibration_not_running()
    await worker.shutdown()
    return SuccessResponse()


@router.post(
    "/clear_commands",
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("robot:control")),
        Depends(check_robot_enabled),
    ],
    summary="Clear queued robot commands",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def clear_robot_commands(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """Clear queued robot commands.

    Removes waiting commands through the worker; requires `robot:control`. An active bin
    workflow or laser calibration prevents this mutation. Read durable command results to
    reconcile interrupted work.
    """
    await _ensure_laser_auto_calibration_not_running()
    await worker.clear_commands()
    return SuccessResponse()


@router.post(
    "/gripper",
    dependencies=[Depends(get_current_user)],
    summary="Grip or release directly",
    responses=response_docs(errors=(401, 409, 503)),
)
async def control_gripper(
    state: Literal["grip", "release"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """Grip or release directly.

    Performs an immediate authenticated gripper operation using the `state` query parameter.
    Changes physical hardware. Blocked while laser auto-calibration runs or another operation
    holds the manual-motion guard.
    """
    await _ensure_laser_auto_calibration_not_running()

    async with manual_motion_guard(f"gripper:{state}", worker=worker):
        if state == "release":
            await worker.release_gripper()
        else:
            await worker.grip()
    return SuccessResponse()


@router.post(
    "/safety/acknowledge",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Acknowledge a cleared safety interruption",
    responses=response_docs(errors=(401, 409, 503)),
)
async def acknowledge_safety_guard(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> SuccessResponse:
    """Acknowledge a cleared safety interruption.

    Acknowledges the worker safety guard only after all interruption sources are clear.
    Returns `409` with `active_sources` while an interruption remains active. This does not
    bypass sensors or home the robot.
    """
    result = await worker.acknowledge_safety_guard()

    if result["accepted"] is False:
        safety = result.get("safety", {})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Cannot acknowledge: safety interruption is still active",
                "code": "safety_interruption_active",
                "active_sources": safety.get("active_sources", []),
            },
        )

    return SuccessResponse()


@router.post(
    "/safety/override",
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("robot:control")),
        Depends(check_robot_enabled),
    ],
    summary="Set the safety guard bypass",
    responses=response_docs(model=SafetyStatus, errors=(401, 403, 503)),
)
async def override_safety_guard(
    request: SafetyGuardOverrideRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict[str, Any]:
    """Set the safety guard bypass.

    Engineering control requiring `robot:control`. `bypass=true` disables the worker safety
    guard; false restores it. Returns the resulting safety state. This changes physical
    safety behavior and must only be used in a controlled engineering procedure.
    """
    return await worker.set_safety_guard_bypassed(request.bypass)


@router.post(
    "/safety/override/component",
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("robot:control")),
        Depends(check_robot_enabled),
    ],
    summary="Set a component safety bypass",
    responses=response_docs(model=SafetyStatus, errors=(401, 403, 503)),
)
async def override_safety_component(
    request: SafetyComponentOverrideRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict[str, Any]:
    """Set a component safety bypass.

    Engineering control requiring `robot:control`. Changes bypass for `light_curtain` or
    `cabinet_doors` and returns the resulting safety state. Restore the component after the
    controlled diagnostic procedure.
    """
    return await worker.set_safety_component_bypassed(request.component, request.bypass)


@router.get(
    "/debug/scanner/status",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Read scanner diagnostics",
    responses=response_docs(model=ScannerStatusResponse, errors=(401, 503)),
)
async def get_scanner_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Read scanner diagnostics.

    Reports scanner connection and enabled state from the initialized worker. This does not
    trigger a hardware scan. Requires an authenticated user and an enabled robot.
    """
    return worker.get_scanner_status()


@router.post(
    "/debug/scanner/scan",
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Inject a barcode scan event",
    responses=response_docs(errors=(401, 403, 503)),
)
async def emit_scanner_scan_event(
    request: LabelScanRequest,
    worker: RobotWorker | None = Depends(get_command_worker_for_mode),
) -> SuccessResponse:
    """Inject a barcode scan event.

    Publishes a `label_scanned` event as if the scanner had read `barcode`; requires
    `robot:control`. Available in mock mode as well as hardware modes. The event can advance
    a waiting bin workflow, so this is not a read-only scanner test.
    """
    payload = {"kind": "label_scanned", "payload": request.barcode}
    try:
        if get_operating_mode() == OperatingMode.mock:
            # Mock mode does not run a RobotEventListener. Route the scan through
            # the same handler directly so bin workflow events still use the mock
            # runtime's SSE publisher.
            await process_robot_event(payload, get_mock_robot_runtime())
        else:
            if worker is None:
                raise RuntimeError("Robot worker is unavailable")
            await worker.publish_event(payload)
    except Exception as exc:
        logging.error("Failed to publish label_scanned event: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to publish label_scanned event",
        ) from exc
    return SuccessResponse()


@router.get(
    "/debug/scale",
    dependencies=[Depends(get_current_user)],
    summary="Read live scale registers",
    responses=response_docs(model=ScaleReadingResponse, errors=(401, 404, 503)),
)
async def get_scale_reading(
    worker: RobotWorker | None = Depends(get_command_worker_for_mode),
) -> dict:
    """Read live scale registers.

    Reads current scale weight in grams, stability/validity flags, decimal position, and raw
    registers. This is live telemetry, not a command-correlated result or inventory update.
    Mock mode returns a deterministic 350-gram reading. A disabled scale returns `404`;
    communication failure returns `503`.
    """
    if get_operating_mode() == OperatingMode.mock:
        return {"weight_grams": 350.0, "decimal_position": 1,
                "status": {"stable": True, "valid": True, "zero": False, "overload": False, "raw": 17},
                "raw_registers": {"weight_hi": 0, "weight_lo": 3500, "decimals": 1, "status": 17}}
    try:
        assert worker is not None
        data = await worker.get_scale_data()
        weight_abs = abs(data["weight"])
        return {
            "weight_grams": weight_abs,
            "decimal_position": data["decimal_position"],
            "status": data["status"],
            "raw_registers": data["raw_registers"],
        }
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to read scale: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale: {exc}",
        ) from exc


@router.get(
    "/debug/scale/calibration/status",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Read scale calibration status",
    responses=response_docs(model=ScaleCalibrationStatusResponse, errors=(401, 404, 503)),
)
async def get_scale_calibration_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Read scale calibration status.

    Reads the physical scale calibration status through the worker. A disabled scale returns
    `404`; unavailable hardware or a failed read returns `503`.
    """
    try:
        return await worker.get_scale_calibration_status()
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to read scale calibration status: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale calibration status: {exc}",
        ) from exc


@router.get(
    "/debug/scale/calibration/params",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Read scale calibration parameters",
    responses=response_docs(model=ScaleCalibrationParameters, errors=(401, 404, 503)),
)
async def get_scale_calibration_params(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Read scale calibration parameters.

    Reads physical scale range, calibration weight, precision, and division settings. These
    settings belong to the hardware scale, not inventory container-mass fields.
    """
    try:
        return await worker.get_scale_calibration_params()
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to read scale calibration params: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale calibration params: {exc}",
        ) from exc


@router.post(
    "/debug/scale/calibration/params",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Write scale calibration parameters",
    responses=response_docs(model=BooleanSuccess, errors=(401, 404, 503)),
)
async def set_scale_calibration_params(
    request: ScaleCalibrationParamsRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Write scale calibration parameters.

    Writes the supplied range, known weight, precision, and division to the physical scale.
    Inspect `success` in the response. An active bin workflow prevents mutation; this does
    not update item mass.
    """
    try:
        ok = await worker.set_scale_calibration_params(
            full_scale=request.full_scale,
            calibration_weight=request.calibration_weight,
            precision=request.precision,
            division=request.division,
        )
        return {"success": ok}
    except HTTPException:
        raise
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to set scale calibration params: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to set scale calibration params: {exc}",
        ) from exc


@router.post(
    "/debug/scale/calibration/zero",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Calibrate the scale zero point",
    responses=response_docs(model=ScaleCalibrationResult, errors=(401, 404, 503)),
)
async def calibrate_scale_zero(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Calibrate the scale zero point.

    Runs the hardware zero-calibration operation. Prepare the scale according to the
    engineering calibration procedure before calling. An active bin workflow prevents
    mutation. The response reports the hardware calibration result.
    """
    try:
        return await worker.calibrate_scale_zero()
    except HTTPException:
        raise
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to calibrate scale zero: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to calibrate scale zero: {exc}",
        ) from exc


@router.post(
    "/debug/scale/calibration/weight",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Calibrate with a known weight",
    responses=response_docs(model=ScaleCalibrationResult, errors=(401, 404, 503)),
)
async def calibrate_scale_weight(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Calibrate with a known weight.

    Runs span calibration using the configured known calibration weight. Place the reference
    weight on the scale before calling. An active bin workflow prevents mutation; this does
    not change inventory mass.
    """
    try:
        return await worker.calibrate_scale_weight()
    except HTTPException:
        raise
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to calibrate scale weight: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to calibrate scale weight: {exc}",
        ) from exc


@router.post(
    "/debug/scale/calibration/tare",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Tare the physical scale",
    responses=response_docs(model=BooleanSuccess, errors=(401, 404, 503)),
)
async def scale_tare(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tare the physical scale.

    Sets the scale tare and returns a `success` flag. This changes the scale reading
    baseline; it does not set an inventory item's `tare_weight`. An active bin workflow
    prevents mutation.
    """
    try:
        ok = await worker.scale_tare()
        return {"success": ok}
    except HTTPException:
        raise
    except ConfigError as exc:
        _raise_scale_config_error(exc)
    except Exception as exc:
        if _is_scale_not_enabled(exc):
            raise _scale_not_enabled_exception() from exc

        logging.error("Failed to tare scale: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to tare scale: {exc}",
        ) from exc


@router.get(
    "/debug/motors",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="List configured motors",
    responses=response_docs(model=MotorsResponse, errors=(401, 503)),
)
async def get_motors() -> dict:
    """List configured motors.

    Lists motor identifiers and configuration used by the diagnostic routes. Use returned
    identifiers as `motor_name`; this endpoint does not command motion.
    """
    tracker = get_init_tracker()
    try:
        worker = await get_robot_worker()
        info = worker.get_motor_info()
    except ValueError:
        robot = get_robot_instance()
        info = robot.get_motor_info() if robot is not None else {"io": [], "tool": []}

    snapshot = tracker.snapshot()
    response: dict[str, object] = dict(info)
    response["init_phase"] = snapshot["phase"]
    response["init_components"] = snapshot["components"]
    return response


@router.post(
    "/debug/motors/{motor_name}/crash",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Inject a motor crash",
    responses=response_docs(model=MotorOperationResponse, errors=(401, 404, 503)),
)
async def simulate_motor_crash(
    motor_name: str,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Inject a motor crash.

    Engineering fault-injection endpoint that sets the selected motor's crash state. The
    worker can react as it would to a real crash. Unknown motor names return `404`.
    """
    success = await worker.simulate_motor_crash(motor_name)

    if success is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{motor_name}' not found",
        )

    return {"success": True, "motor": motor_name}


@router.post(
    "/debug/motors/{motor_name}/clear",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Clear a motor alarm",
    responses=response_docs(model=MotorOperationResponse, errors=(401, 404, 503)),
)
async def clear_motor_alarm(
    motor_name: str,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Clear a motor alarm.

    Clears the hardware alarm and matching worker crash state, then refreshes status. This
    does not home the motor. Unknown names return `404`; a failed hardware operation returns
    `503`.
    """
    motor = worker._robot._get_motor_by_name(motor_name)

    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{motor_name}' not found",
        )

    try:
        async with manual_motion_guard(f"motor_clear:{motor_name}", worker=worker):
            was_simulated_fault = bool(getattr(motor, "_simulated_fault", False))
            await motor.clear_alarm()
            if was_simulated_fault is True:
                motor._simulated_fault = False
                worker.clear_needs_home_axes([motor_name])
                if worker.needs_home() and worker.needs_home_axes() == []:
                    if _reason_matches_motor(worker.needs_home_reason(), motor_name, motor):
                        worker.clear_needs_home()
                _clear_matching_motor_crash_state(worker, motor_name, motor)
            await worker.get_status_snapshot(force_refresh=True)
            return {"success": True, "motor": motor_name}
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Motor alarm clear failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(f"Motor alarm clear failed: {exc}", "motor_alarm_clear_failed"),
        ) from exc


@router.post(
    "/debug/motors/{motor_name}/home",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Home one motor",
    responses=response_docs(model=MotorOperationResponse, errors=(400, 401, 403, 404, 503)),
)
async def home_motor(
    motor_name: str,
    skip_pre_home_approach: bool = False,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Home one motor.

    Runs the selected motor's homing operation with manual-motion guards. This moves
    hardware and may also position the tool for safe rotation. `skip_pre_home_approach=true`
    is restricted to the Y axis in development mode. Unknown names return `404`; motion that
    another operation already holds returns `409`.
    """
    if skip_pre_home_approach is True:
        if get_operating_mode() != OperatingMode.development:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Skipping the pre-home approach is only available in development mode",
            )
        if motor_name != "y_axis":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Skipping the pre-home approach is only supported for y_axis diagnostics",
            )

    motor = worker._robot._get_motor_by_name(motor_name)

    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{motor_name}' not found",
        )

    try:
        async with manual_motion_guard(f"motor_home:{motor_name}", worker=worker):
            if motor_name == "rotate":
                original_x = await worker._robot._tool.ensure_safe_rotation_x_position()
                try:
                    await worker._robot._tool.ensure_safe_rotation_stage_extension()
                    await motor.clear_alarm()
                    await motor.home()
                finally:
                    await worker._robot._tool.restore_safe_rotation_x_position(original_x)
            else:
                await motor.clear_alarm()
                if skip_pre_home_approach is True:
                    await motor.home(skip_pre_home_approach=True)
                else:
                    await motor.home()
            if motor_name == "rotate":
                await worker._robot._tool.rotate_to_home_orientation(
                    should_ensure_safe_stage_extension=False,
                )
            worker.clear_needs_home_axes([motor_name])
            if worker.needs_home() and worker.needs_home_axes() == []:
                if _reason_matches_motor(worker.needs_home_reason(), motor_name, motor):
                    worker.clear_needs_home()
            _clear_matching_motor_crash_state(worker, motor_name, motor)
            await worker.get_status_snapshot(force_refresh=True)
            return {"success": True, "motor": motor_name}
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Motor home failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(f"Motor home failed: {exc}", "motor_home_failed"),
        ) from exc


@router.get(
    "/debug/power",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Inspect switched power rails",
    responses=response_docs(model=PowerRailsResponse, errors=(401, 503)),
)
async def get_power_rails() -> dict:
    """Read the four switched power rails.

    Reports what each rail feeds, the axes that lose their encoder reference when it is
    cut, and the current output state. `powered` is `null` when the rail cannot be read.
    Cut power only through the rail cycle operation: driving the output directly drops
    the drives without reconfiguring or re-homing the axes that lose their reference.
    """
    states: dict[str, bool | None] = {rail: None for rail in POWER_RAILS}
    try:
        worker = await get_robot_worker()
        states = worker._robot.get_power_rail_states()
    except ValueError:
        robot = get_robot_instance()
        if robot is not None:
            states = robot.get_power_rail_states()

    return {
        "hold_seconds": POWER_CYCLE_HOLD_S,
        "rails": [
            {
                "rail": rail,
                "supplies": spec.supplies,
                "axes": list(spec.axes),
                "gripper": spec.gripper,
                "powered": states.get(rail),
            }
            for rail, spec in POWER_RAILS.items()
        ],
    }


@router.post(
    "/debug/power/{rail}/cycle",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Power-cycle a switched rail",
    responses=response_docs(model=PowerCycleResponse, errors=(401, 404, 409, 503)),
)
async def power_cycle_rail(
    rail: str,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    """Cut a switched rail for a few seconds, then restore it.

    Drive parameters that only latch at power-on need this -- any DI/DO function
    change stays inert until its drive restarts. Returns once power is back; the
    hardware is still booting, so re-home the axes it dropped before moving. An
    active command or queued work returns `409`; a tray workflow that is idle between
    steps does not block the cycle.
    """
    if rail not in POWER_RAILS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Power rail '{rail}' not found",
        )

    try:
        async with manual_motion_guard(f"power_cycle:{rail}", worker=worker):
            cut_axes = list(POWER_RAILS[rail].axes)

            def _mark_axes_for_homing() -> None:
                if cut_axes:
                    worker.set_needs_home(f"power cycle: {rail}", cut_axes)

            axes = await worker._robot.power_cycle_rail(rail, on_cut=_mark_axes_for_homing)

            await worker.get_status_snapshot(force_refresh=True)
            return {"success": True, "rail": rail, "axes_needing_home": list(axes)}
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Power cycle failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(f"Power cycle failed: {exc}", "power_cycle_failed"),
        ) from exc


@router.post(
    "/debug/motors/probe",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Probe motor responsiveness",
    responses=response_docs(errors=(401, 503)),
)
async def probe_motors(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> MotorProbeResponse:
    """Probe motor responsiveness.

    Performs diagnostic position reads without commanding movement. Returns `busy=true` and
    a reason instead of probing when commands, manual motion, or demo cycling own the robot.
    Each result reports responsiveness independently.
    """
    busy_reason = _probe_busy_reason(worker)
    if busy_reason is not None:
        return MotorProbeResponse(busy=True, reason=busy_reason)

    try:
        async with manual_motion_guard("motor_probe", worker=worker):
            motor_names = worker.get_motor_names()
            results: List[MotorProbeResult] = []

            all_motors: List[str] = []
            for section_motors in motor_names.values():
                all_motors.extend(section_motors)

            for motor_name in all_motors:
                motor = worker._robot._get_motor_by_name(motor_name)

                if motor is None:
                    results.append(MotorProbeResult(
                        motor_name=motor_name,
                        responsive=False,
                        error="motor not found",
                    ))
                    continue

                try:
                    position = await motor.get_position()
                    results.append(MotorProbeResult(
                        motor_name=motor_name,
                        responsive=True,
                        position_mm=position,
                    ))
                except Exception as exc:
                    results.append(MotorProbeResult(
                        motor_name=motor_name,
                        responsive=False,
                        error=str(exc),
                    ))

            return MotorProbeResponse(results=results)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise

        reason = exc.detail.get("reason") if isinstance(exc.detail, dict) else None
        return MotorProbeResponse(busy=True, reason=reason)


@router.post(
    "/debug/motors/{motor_name}/move",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Move one motor to a position",
    responses=response_docs(model=MotorOperationResponse, errors=(401, 404, 503)),
)
async def move_motor(
    motor_name: str,
    request: MotorMoveRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Move one motor to a position.

    Moves the selected motor to `position_mm` using the manual-motion guard. Rotation may
    require associated stage positioning. Unknown names return `404`; motion that another
    operation already holds returns `409`; hardware failures return `503`.
    """
    motor = worker._robot._get_motor_by_name(motor_name)

    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{motor_name}' not found",
        )

    try:
        async with manual_motion_guard(f"motor_move:{motor_name}", worker=worker):
            if motor_name == "rotate":
                if await worker._robot._tool.is_rotation_at_position(request.position_mm):
                    return {"success": True, "motor": motor_name, "position_mm": request.position_mm}

                original_x = await worker._robot._tool.ensure_safe_rotation_x_position()
                try:
                    await worker._robot._tool.ensure_safe_rotation_stage_extension()
                    await motor.move_to_position(request.position_mm)
                finally:
                    await worker._robot._tool.restore_safe_rotation_x_position(original_x)
            else:
                await motor.move_to_position(request.position_mm)
            return {"success": True, "motor": motor_name, "position_mm": request.position_mm}
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning("Motor move failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(f"Motor move failed: {exc}", "motor_move_failed"),
        ) from exc


@router.post(
    "/debug/door",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Operate the IO box door",
    responses=response_docs(model=DoorOperationResponse, errors=(401, 503)),
)
async def control_door(
    action: Literal["open", "close", "home"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Operate the IO box door.

    Directly opens, closes, or homes the IO door using the `action` query parameter. Runs
    under the manual-motion guard and cannot interrupt an active bin workflow. Hardware
    failures return `503`.
    """
    try:
        async with manual_motion_guard(f"door:{action}", worker=worker):
            if action == "open":
                await worker._robot._io.open_door()
            elif action == "close":
                await worker._robot._io.close_door()
            elif action == "home":
                await worker._robot._io.home_door()

            return {"success": True, "action": action}
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Door control failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(f"Door {action} failed: {exc}", "door_control_failed"),
        ) from exc



async def get_occupied_cells_for_overlay() -> set[tuple[str, int, int]]:
    """Return occupied cells for the status overlay.

    Reads are cached per commit generation (see `core.occupancy_cache`): frame
    generation reuses the last snapshot, so a stream no longer issues the same
    query pair once per status push inside its long-lived transaction.
    """
    return await occupancy_cache.get_occupied_cells()


@router.get(
    "/status",
    dependencies=[Depends(get_relay_or_current_user)],
    summary="Stream robot status",
    responses=response_docs(
        errors=(401, 503),
        existing=stream_docs(
            "text/event-stream",
            "Live JSON status snapshots in SSE data frames. Heartbeat comments are not JSON.",
            'data: {"mode":"mock","running":true,"current_command":null,"queue_length":0}\n\n',
        ),
    ),
    response_class=Response,
)
async def stream_sse() -> EventSourceResponse:
    """Stream robot status.

    Server-Sent Events (`text/event-stream`). Each `data:` value is a JSON status snapshot
    with top-level `mode`; initialization progress may appear before the worker is ready.
    Database item locations are overlaid on storage occupancy. Subscribe for current state
    rather than durable command outcomes.

    Supports bearer authentication or the user access token in `?token=` for clients unable
    to set headers. Sends heartbeat comments while connected. Reconnect to obtain current
    status; the stream does not provide historical replay. Mock mode uses the same transport
    with simulated hardware state.
    """
    if get_operating_mode() == OperatingMode.mock:
        runtime = get_mock_robot_runtime()
        status_queue, snapshot = await runtime.subscribe_status()

        async def mock_status_generator() -> AsyncGenerator[dict[str, str], None]:
            try:
                yield {"data": snapshot}
                while True:
                    yield {"data": await status_queue.get()}
            finally:
                await runtime.unsubscribe_status(status_queue)

        return EventSourceResponse(
            mock_status_generator(),
            ping=15,
            send_timeout=30,
            headers={"Cache-Control": "no-cache"},
        )

    tracker = get_init_tracker()
    tracker_queue = await tracker.subscribe()

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        worker: Optional[RobotWorker] = None
        status_queue: Optional[asyncio.Queue[str]] = None
        waiters: dict[asyncio.Task[str], str] = {}

        async def cancel_waiters(tasks: Iterable[asyncio.Task[str]]) -> None:
            pending_tasks = [task for task in tasks if not task.done()]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        try:
            yield {"event": "init_progress", "data": tracker.snapshot_json()}

            while True:
                if worker is None:
                    try:
                        worker = await get_robot_worker()
                    except ValueError:
                        worker = None

                    if worker is not None:
                        status_queue, _ = await worker.subscribe_status()
                        raw_payload = await worker.get_status_snapshot(force_refresh=True)
                        if raw_payload is not None:
                            occupied_cells = await get_occupied_cells_for_overlay()
                            yield {"data": inject_mode_field(overlay_db_occupancy(raw_payload, occupied_cells))}

                waiters = {
                    asyncio.ensure_future(tracker_queue.get()): "init",
                }
                if status_queue is not None:
                    waiters[asyncio.ensure_future(status_queue.get())] = "status"

                timeout = None if worker is not None else 1.0
                done, pending = await asyncio.wait(
                    waiters.keys(),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                await cancel_waiters(pending)

                for task in done:
                    kind = waiters[task]
                    try:
                        data = task.result()
                    except Exception:
                        continue

                    if kind == "init":
                        yield {"event": "init_progress", "data": data}
                    else:
                        occupied_cells = await get_occupied_cells_for_overlay()
                        yield {"data": inject_mode_field(overlay_db_occupancy(data, occupied_cells))}
                waiters = {}
        finally:
            await cancel_waiters(waiters.keys())
            await tracker.unsubscribe(tracker_queue)
            if worker is not None and status_queue is not None:
                await worker.unsubscribe_status(status_queue)

    return EventSourceResponse(
        event_generator(),
        ping=15,
        send_timeout=30,
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/events",
    dependencies=[Depends(get_relay_or_current_user)],
    summary="Stream robot events and command results",
    responses=response_docs(
        errors=(401, 503),
        existing=stream_docs(
            "text/event-stream",
            "Live JSON robot events in SSE data frames; command, scanner, safety and workflow events share this stream.",
            'data: {"message":"success","command":"store_item","command_id":"00000000-0000-4000-8000-000000000001","barcode":"880000000001","location":{"side":"left_module","shelf_idx":0,"cell_idx":0},"weight":350}\n\n',
        ),
    ),
    response_class=Response,
)
async def stream_events() -> EventSourceResponse:
    """Stream robot events and command results.

    Server-Sent Events with JSON in each `data:` frame. Subscribe before submitting
    commands: this is a live stream without historical replay. Events include terminal
    command results, scanner events, and bin-workflow updates; dispatch by `command`,
    `message`, or `kind`, rather than assuming every frame is a completion.

    A terminal result includes `command_id`; use durable command polling after a disconnect.
    `weight`, when present, is the cycle's gross reading in grams, not container net mass.
    Physical retrieval currently supplies no weight reading. Inventory changes are
    committed before an event is forwarded. User tokens can be supplied through the
    bearer header or `?token=`.
    """
    if get_operating_mode() == OperatingMode.mock:
        runtime = get_mock_robot_runtime()
        mock_event_queue = await runtime.subscribe_events()

        async def mock_event_generator() -> AsyncGenerator[dict[str, str], None]:
            try:
                while True:
                    yield {"data": await mock_event_queue.get()}
            finally:
                await runtime.unsubscribe_events(mock_event_queue)

        return EventSourceResponse(mock_event_generator())

    event_queue: Optional[asyncio.Queue[str]] = None
    worker: Optional[RobotWorker]

    try:
        worker = await get_robot_worker()
    except ValueError:
        worker = None

    if worker is not None:
        event_queue = await worker.subscribe_events()

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            if event_queue is None:
                return

            forwarded = 0
            while True:
                message = await event_queue.get()
                forwarded += 1
                # The event listener commits DB state after the worker emits, so
                # wait for that work to settle before clients observe the event.
                await event_processing_tracker.wait_until_processed(
                    forwarded, EVENT_FORWARD_WAIT_TIMEOUT_S
                )
                yield {"data": message}
        finally:
            if worker is not None and event_queue is not None:
                await worker.unsubscribe_events(event_queue)

    return EventSourceResponse(event_generator())
