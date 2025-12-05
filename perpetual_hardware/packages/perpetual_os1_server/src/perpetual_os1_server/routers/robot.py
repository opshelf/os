from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from pydantic import BaseModel, Field
from perpetual_os1.robot import RobotCommands, RetrieveItemCommand, StoreItemCommand
import logging
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.robot_worker import RobotWorker
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from perpetual_os1_server.core.robot import get_robot_client, get_robot_worker
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1_server.settings import get_settings
from perpetual_os1_server.database.session import get_db, AsyncSessionLocal
from perpetual_os1_server.database.models import Item, ItemLocation, User
from .auth_deps import (
    get_current_user,
    get_current_user_optional,
    require_permissions,
    can_user_retrieve_item,
)

router = APIRouter(tags=["robot"])


def overlay_db_occupancy(payload_json: str, occupied: set[tuple[str, int, int]]) -> str:
    payload = json.loads(payload_json)
    storage = payload.get("robot_status", {}).get("storage", {})

    for side in ("left_module", "right_module"):
        for shelf_idx, shelf in enumerate(storage.get(side, [])):
            for cell_idx, cell in enumerate(shelf.get("cells", [])):
                key = (side, shelf_idx, cell_idx)
                if key in occupied:
                    cell["status"] = "occupied"
                elif cell.get("status") == "occupied":
                    cell["status"] = "available"

    return json.dumps(payload)


class LabelScanRequest(BaseModel):
    code: str = Field(..., min_length=1)


class ScaleCalibrationParamsRequest(BaseModel):
    full_scale: int = Field(..., description="Full scale value in grams (e.g., 6000 for 6kg)")
    calibration_weight: int = Field(..., description="Known calibration weight in grams")
    precision: int = Field(default=1, ge=0, le=3, description="Decimal precision (0-3)")
    division: int = Field(default=1, ge=1, description="Division value")


async def check_robot_enabled() -> None:
    settings = get_settings()
    if not settings.robot.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not enabled or configured",
        )


async def get_robot_client_or_error() -> RobotClient:
    try:
        return await get_robot_client()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not initialized or failed to start",
        ) from e


async def get_robot_worker_or_error() -> RobotWorker:
    try:
        return await get_robot_worker()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not initialized or failed to start",
        ) from e


@router.post("/commands", dependencies=[Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def robot_commands(
    command: RobotCommands,
    client: RobotClient = Depends(get_robot_client_or_error),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> SuccessResponse:
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

    await client.send_command(command)
    return SuccessResponse()


@router.post("/start", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def start_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await worker.start()
    return SuccessResponse()


@router.post("/stop", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def stop_robot(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await worker.shutdown()
    return SuccessResponse()


@router.post("/clear_commands", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control")), Depends(check_robot_enabled)])
async def clear_robot_commands(worker: RobotWorker = Depends(get_robot_worker_or_error)) -> SuccessResponse:
    await worker.clear_commands()
    return SuccessResponse()


async def _publish_label_scanned(code: str, settings) -> None:
    nats_config = RobotNatsConfiguration(domain=settings.nats.domain, port=settings.nats.port)
    nats_client = NATS()
    try:
        await nats_client.connect(servers=[nats_config.connection_string()])
        payload = json.dumps({"kind": "label_scanned", "payload": code}).encode()
        await nats_client.publish(nats_config.event_queue, payload)
    finally:
        if nats_client.is_connected:
            try:
                await nats_client.drain()
            except Exception:
                await nats_client.close()


@router.post(
    "/debug/label_scan",
    dependencies=[Depends(require_permissions("robot:control")), Depends(check_robot_enabled)],
)
async def emit_label_scanned_event(request: LabelScanRequest) -> SuccessResponse:
    settings = get_settings()
    try:
        await _publish_label_scanned(request.code, settings)
    except Exception as exc:
        logging.error("Failed to publish label_scanned event: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to publish label_scanned event",
        ) from exc
    return SuccessResponse()


@router.get("/debug/scale", dependencies=[Depends(check_robot_enabled)])
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


@router.get("/debug/scale/calibration/status", dependencies=[Depends(check_robot_enabled)])
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


@router.get("/debug/scale/calibration/params", dependencies=[Depends(check_robot_enabled)])
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


@router.post("/debug/scale/calibration/params", dependencies=[Depends(check_robot_enabled)])
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


@router.post("/debug/scale/calibration/zero", dependencies=[Depends(check_robot_enabled)])
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


@router.post("/debug/scale/calibration/weight", dependencies=[Depends(check_robot_enabled)])
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


async def get_occupied_cells_from_db() -> set[tuple[str, int, int]]:
    async with AsyncSessionLocal() as session:
        service = StorageService(session)
        rows = await service.get_all_occupied_cells()
        return {(row[0], row[1], row[2]) for row in rows}


async def nats_consumer(stop_event: asyncio.Event) -> AsyncGenerator[str, None]:
    nats_client = NATS()
    message_queue: asyncio.Queue[str] = asyncio.Queue()

    async def message_handler(msg: Msg) -> None:
        await message_queue.put(msg.data.decode())

    sub: Optional[Subscription] = None
    try:
        await nats_client.connect(get_settings().nats.connection_string())
        sub = await nats_client.subscribe("robot.status", cb=message_handler)

        while not stop_event.is_set():
            try:
                message = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                occupied_cells = await get_occupied_cells_from_db()
                yield overlay_db_occupancy(message, occupied_cells)
            except asyncio.TimeoutError:
                continue
    finally:
        if sub is not None:
            await sub.unsubscribe()
        await nats_client.close()


async def event_consumer(stop_event: asyncio.Event) -> AsyncGenerator[str, None]:
    nats_client = NATS()
    message_queue: asyncio.Queue[str] = asyncio.Queue()

    async def message_handler(msg: Msg) -> None:
        await message_queue.put(msg.data.decode())

    sub: Optional[Subscription] = None
    subject = RobotNatsConfiguration().event_queue
    try:
        await nats_client.connect(get_settings().nats.connection_string())
        sub = await nats_client.subscribe(subject, cb=message_handler)

        while not stop_event.is_set():
            try:
                message = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                yield message
            except asyncio.TimeoutError:
                continue
    finally:
        if sub is not None:
            await sub.unsubscribe()
        await nats_client.close()


@router.get("/status")
async def stream_sse(
    current_user: dict | None = Depends(get_current_user_optional)
) -> EventSourceResponse:
    stop_event = asyncio.Event()

    initial_payload: Optional[str] = None
    try:
        worker = await get_robot_worker()
    except ValueError:
        worker = None

    if worker is not None:
        raw_payload = await worker.get_status_snapshot(force_refresh=True)
        if raw_payload is not None:
            occupied_cells = await get_occupied_cells_from_db()
            initial_payload = overlay_db_occupancy(raw_payload, occupied_cells)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            if initial_payload is not None:
                yield {"data": initial_payload}

            async for message in nats_consumer(stop_event):
                yield {"data": message}
        finally:
            stop_event.set()

    return EventSourceResponse(event_generator())


@router.get("/events")
async def stream_events(
    current_user: dict | None = Depends(get_current_user_optional)
) -> EventSourceResponse:
    stop_event = asyncio.Event()

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for message in event_consumer(stop_event):
                yield {"data": message}
        finally:
            stop_event.set()

    return EventSourceResponse(event_generator())
