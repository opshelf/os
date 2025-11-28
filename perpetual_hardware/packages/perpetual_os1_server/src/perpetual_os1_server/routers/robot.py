from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from perpetual_os1.robot import RobotCommands, RetrieveItemCommand
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.robot_worker import RobotWorker
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from perpetual_os1_server.core.robot import get_robot_client, get_robot_worker
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1_server.settings import get_settings
from perpetual_os1_server.database.session import get_db
from perpetual_os1_server.database.models import Item, ItemLocation, User
from .auth_deps import (
    get_current_user,
    get_current_user_optional,
    require_permissions,
    can_user_retrieve_item,
)

router = APIRouter(tags=["robot"])


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
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    if isinstance(command, RetrieveItemCommand):
        location_data = command.location or {}
        shelf_idx = location_data.get("shelf_idx")
        cell_idx = location_data.get("cell_idx")

        if shelf_idx is None or cell_idx is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid location: shelf_idx and cell_idx are required",
            )

        result = await db.execute(
            select(Item)
            .join(ItemLocation)
            .where(
                ItemLocation.shelf_idx == shelf_idx,
                ItemLocation.cell_idx == cell_idx,
            )
        )
        item = result.scalar_one_or_none()

        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No item found at location (shelf: {shelf_idx}, cell: {cell_idx})",
            )

        if can_user_retrieve_item(current_user, item) is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to retrieve this item",
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
        initial_payload = await worker.get_status_snapshot(force_refresh=True)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            if initial_payload is not None:
                yield {"data": initial_payload}
            async for message in nats_consumer(stop_event):
                yield {"data": message}
        finally:
            stop_event.set()

    return EventSourceResponse(event_generator())


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
