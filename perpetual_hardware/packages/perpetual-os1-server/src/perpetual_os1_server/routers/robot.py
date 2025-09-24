from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from perpetual_os1.robot import RobotCommands
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.robot_worker import RobotWorker
from sse_starlette.sse import EventSourceResponse

from perpetual_os1_server.core.robot import get_robot_client, get_robot_worker
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1_server.settings import get_settings
from .auth_deps import (
    get_current_user,
    get_current_user_optional,
    require_permissions,
)

router = APIRouter(tags=["robot"])


@router.post("/commands", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))])
async def robot_commands(
    command: RobotCommands, client: RobotClient = Depends(get_robot_client)
) -> SuccessResponse:
    await client.send_command(command)
    return SuccessResponse()


@router.post("/start", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))])
async def start_robot(worker: RobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.start()
    return SuccessResponse()


@router.post("/stop", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))])
async def stop_robot(worker: RobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.shutdown()
    return SuccessResponse()


@router.post("/clear_commands", dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))])
async def clear_robot_commands(worker: RobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
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
    current_user = Depends(get_current_user_optional)
) -> EventSourceResponse:
    stop_event = asyncio.Event()

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for message in nats_consumer(stop_event):
                yield {"data": message}
        finally:
            stop_event.set()

    return EventSourceResponse(event_generator())
