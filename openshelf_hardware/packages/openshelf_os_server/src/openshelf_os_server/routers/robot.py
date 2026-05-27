from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncGenerator, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from openshelf_os.robot import RobotCommands, RetrieveItemCommand, StoreItemCommand
import logging
from openshelf_os.robot_worker import RobotWorker
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from openshelf_os_server.core.robot import get_robot_health, get_robot_worker
from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.manual_motion import manual_motion_guard, robot_command_enqueue_guard
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.routers.common import SuccessResponse
from openshelf_os_server.settings import get_settings, get_operating_mode
from openshelf_os_server.database.session import get_db, AsyncSessionLocal
from openshelf_os_server.database.models import Item, ItemLocation, User
from .auth_deps import (
    get_current_user,
    require_permissions,
    can_user_retrieve_item,
)

router = APIRouter(tags=["robot"])

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)


def _error_detail(message: str, code: str) -> dict[str, str]:
    return {"message": message, "code": code}


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
    code: str = Field(..., min_length=1)


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


class MotorMoveRequest(BaseModel):
    position_mm: float = Field(..., description="Target position in millimeters")


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


@router.post("/commands", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def robot_commands(
    command: RobotCommands,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    await _ensure_laser_auto_calibration_not_running()

    if isinstance(command, StoreItemCommand):
        logging.info("Processing StoreItemCommand: item=%s", command.item)
        item_data = command.item or {}
        upca = item_data.get("upca")

        if upca is not None:
            logging.info("Looking up item by upca: %s", upca)
            result = await db.execute(select(Item).where(Item.upca == upca))
            item = result.scalar_one_or_none()

            if item is not None:
                logging.info("Found item in db: name=%s, dimensions=%s", item.name, item.dimensions)
                item_data["dimensions"] = item.dimensions
                item_data["name"] = item.name
                if item.weight is not None:
                    item_data["weight"] = item.weight
                command = StoreItemCommand(item=item_data)
            else:
                logging.info("Item not found in db for upca: %s", upca)

        logging.info("Final StoreItemCommand: item=%s", command.item)

    if isinstance(command, RetrieveItemCommand):
        try:
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
        await worker.enqueue_command(command)
    return SuccessResponse()


@router.post("/start", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def start_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await _ensure_laser_auto_calibration_not_running()
    await worker.start()
    return SuccessResponse()


@router.post("/stop", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def stop_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await _ensure_laser_auto_calibration_not_running()
    await worker.shutdown()
    return SuccessResponse()


@router.post("/clear_commands", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def clear_robot_commands(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await _ensure_laser_auto_calibration_not_running()
    await worker.clear_commands()
    return SuccessResponse()


@router.post("/emergency_stop", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def emergency_stop_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await worker.emergency_stop("User initiated emergency stop")
    return SuccessResponse()


@router.post("/gripper", dependencies=[Depends(get_current_user)])
async def control_gripper(
    state: Literal["grip", "release"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> SuccessResponse:
    await _ensure_laser_auto_calibration_not_running()

    async with manual_motion_guard(f"gripper:{state}", worker=worker):
        if state == "release":
            await worker.release_gripper()
        else:
            await worker.grip()
    return SuccessResponse()


@router.post("/curtain_guard/acknowledge", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def acknowledge_curtain_guard(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> SuccessResponse:
    accepted = await worker.acknowledge_curtain_guard()

    if accepted is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot acknowledge: light curtain is still blocked",
        )

    return SuccessResponse()


@router.get("/debug/scanner/status", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_scanner_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    return worker.get_scanner_status()


@router.post(
    "/debug/scanner/scan",
    dependencies=[Depends(require_permissions("robot:control")), Depends(check_robot_enabled)],
)
async def emit_scanner_scan_event(
    request: LabelScanRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> SuccessResponse:
    try:
        await worker.publish_event({"kind": "label_scanned", "payload": request.code})
    except Exception as exc:
        logging.error("Failed to publish label_scanned event: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to publish label_scanned event",
        ) from exc
    return SuccessResponse()


@router.get("/debug/scale", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_scale_reading(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        data = await worker.get_scale_data()
        weight_abs = abs(data["weight"])
        return {
            "weight_grams": weight_abs,
            "decimal_position": data["decimal_position"],
            "status": data["status"],
            "raw_registers": data["raw_registers"],
        }
    except Exception as exc:
        logging.error("Failed to read scale: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale: {exc}",
        ) from exc


@router.get("/debug/scale/calibration/status", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_scale_calibration_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        return await worker.get_scale_calibration_status()
    except Exception as exc:
        logging.error("Failed to read scale calibration status: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale calibration status: {exc}",
        ) from exc


@router.get("/debug/scale/calibration/params", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_scale_calibration_params(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        return await worker.get_scale_calibration_params()
    except Exception as exc:
        logging.error("Failed to read scale calibration params: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to read scale calibration params: {exc}",
        ) from exc


@router.post("/debug/scale/calibration/params", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def set_scale_calibration_params(
    request: ScaleCalibrationParamsRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        ok = await worker.set_scale_calibration_params(
            full_scale=request.full_scale,
            calibration_weight=request.calibration_weight,
            precision=request.precision,
            division=request.division,
        )
        return {"success": ok}
    except Exception as exc:
        logging.error("Failed to set scale calibration params: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to set scale calibration params: {exc}",
        ) from exc


@router.post("/debug/scale/calibration/zero", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def calibrate_scale_zero(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        return await worker.calibrate_scale_zero()
    except Exception as exc:
        logging.error("Failed to calibrate scale zero: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to calibrate scale zero: {exc}",
        ) from exc


@router.post("/debug/scale/calibration/weight", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def calibrate_scale_weight(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        return await worker.calibrate_scale_weight()
    except Exception as exc:
        logging.error("Failed to calibrate scale weight: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to calibrate scale weight: {exc}",
        ) from exc


@router.post("/debug/scale/calibration/tare", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def scale_tare(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    try:
        ok = await worker.scale_tare()
        return {"success": ok}
    except Exception as exc:
        logging.error("Failed to tare scale: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to tare scale: {exc}",
        ) from exc


@router.get("/debug/motors", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_motors(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    return worker.get_motor_info()


@router.post("/debug/motors/{motor_name}/crash", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def simulate_motor_crash(
    motor_name: str,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
    success = await worker.simulate_motor_crash(motor_name)

    if success is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{motor_name}' not found",
        )

    return {"success": True, "motor": motor_name}


@router.post("/debug/motors/{motor_name}/home", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def home_motor(
    motor_name: str,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
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
                await motor.home()
            if motor_name == "rotate":
                await worker._robot._tool.rotate_to_home_orientation(
                    should_ensure_safe_stage_extension=False,
                )
            worker.clear_needs_home_axes([motor_name])
            if worker.needs_home() and worker.needs_home_axes() == []:
                if _reason_matches_motor(worker.needs_home_reason(), motor_name, motor):
                    worker.clear_needs_home()
            await worker.get_status_snapshot(force_refresh=True)
            return {"success": True, "motor": motor_name}
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Motor home failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_error_detail(f"Motor home failed: {exc}", "motor_home_failed"),
        ) from exc


@router.post("/debug/motors/probe", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def probe_motors(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> Dict[str, List[MotorProbeResult]]:
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

    return {"results": results}


@router.post("/debug/motors/{motor_name}/move", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def move_motor(
    motor_name: str,
    request: MotorMoveRequest,
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
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
        logging.error("Motor move failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_error_detail(f"Motor move failed: {exc}", "motor_move_failed"),
        ) from exc


@router.post("/debug/door", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def control_door(
    action: Literal["open", "close", "home"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> dict:
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
            detail=_error_detail(f"Door {action} failed: {exc}", "door_control_failed"),
        ) from exc


async def get_occupied_cells_from_db() -> set[tuple[str, int, int]]:
    async with AsyncSessionLocal() as session:
        service = StorageService(session)
        rows = await service.get_all_occupied_cells()
        return {(row[0], row[1], row[2]) for row in rows}


@router.get("/status", dependencies=[Depends(get_current_user)])
async def stream_sse() -> EventSourceResponse:
    initial_payload: Optional[str] = None
    status_queue: Optional[asyncio.Queue[str]] = None
    worker: Optional[RobotWorker]

    try:
        worker = await get_robot_worker()
    except ValueError:
        worker = None

    if worker is not None:
        status_queue, current_payload = await worker.subscribe_status()
        raw_payload = current_payload

        if raw_payload is None:
            raw_payload = await worker.get_status_snapshot(force_refresh=True)

        if raw_payload is not None:
            occupied_cells = await get_occupied_cells_from_db()
            initial_payload = inject_mode_field(overlay_db_occupancy(raw_payload, occupied_cells))

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            if initial_payload is not None:
                yield {"data": initial_payload}

            if status_queue is None:
                yield {"event": "robot_unavailable", "data": json.dumps(get_robot_health())}
                return
            while True:
                message = await status_queue.get()
                occupied_cells = await get_occupied_cells_from_db()
                yield {"data": inject_mode_field(overlay_db_occupancy(message, occupied_cells))}
        finally:
            if worker is not None and status_queue is not None:
                await worker.unsubscribe_status(status_queue)

    return EventSourceResponse(
        event_generator(),
        ping=15,
        send_timeout=30,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/events", dependencies=[Depends(get_current_user)])
async def stream_events() -> EventSourceResponse:
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

            while True:
                message = await event_queue.get()
                yield {"data": message}
        finally:
            if worker is not None and event_queue is not None:
                await worker.unsubscribe_events(event_queue)

    return EventSourceResponse(event_generator())
