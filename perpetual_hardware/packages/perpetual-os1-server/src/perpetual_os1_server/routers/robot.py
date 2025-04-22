from fastapi import APIRouter, Depends
from perpetual_os1.robot import RobotCommands, RobotConfiguration
from perpetual_os1.robot_worker import RobotWorker
from perpetual_os1.robot_client import RobotClient
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1_server.core.robot import get_robot_client, get_robot_worker
from typing import List, AsyncGenerator, Optional
import asyncio
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from sse_starlette.sse import EventSourceResponse
from nats.aio.client import Client as NATS
from perpetual_p0c_server.settings import get_settings

router = APIRouter()


@router.post("/commands")
async def robot_commands(
    command: RobotCommands, client: RobotClient = Depends(get_robot_client)
) -> SuccessResponse:
    await client.send_command(command)
    return SuccessResponse()


@router.post("/start")
async def start_robot(worker: RobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.start()
    return SuccessResponse()


@router.post("/stop")
async def stop_robot(worker: RobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.shutdown()
    return SuccessResponse()


@router.post("/clear_commands")
async def clear_robot_commands(
    worker: RobotWorker = Depends(get_robot_worker),
) -> SuccessResponse:
    await worker.clear_commands()
    return SuccessResponse()


@router.get("/configuration")
async def configuration(
    worker: RobotWorker = Depends(get_robot_worker),
) -> RobotConfiguration:
    return worker.get_configuration()


@router.post("/configuration")
async def set_configuration(
    configuration: RobotConfiguration,
    worker: RobotWorker = Depends(get_robot_worker),
) -> SuccessResponse:
    await worker.set_configuration(configuration)
    return SuccessResponse()


async def nats_consumer(stop_event: asyncio.Event) -> AsyncGenerator[str, None]:
    nats_client = NATS()
    message_queue = asyncio.Queue[str]()

    async def message_handler(msg: Msg) -> None:
        await message_queue.put(msg.data.decode())

    sub: Optional[Subscription] = None
    try:
        await nats_client.connect(get_settings().nats.connection_string())
        sub = await nats_client.subscribe("robot.status", cb=message_handler)

        while not stop_event.is_set():
            try:
                # Get the next message from the queue
                message = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                yield message
            except asyncio.TimeoutError:
                # Timeout to check the stop_event periodically
                continue
    finally:
        # Ensure the NATS client disconnects
        if not sub is None:
            await sub.unsubscribe()
        await nats_client.close()


@router.get("/status")
async def stream_sse() -> EventSourceResponse:
    """
    SSE endpoint that streams NATS messages to the client.
    """
    stop_event = asyncio.Event()

    async def event_generator() -> AsyncGenerator:
        try:
            async for message in nats_consumer(stop_event):
                yield {"data": message}
        finally:
            stop_event.set()

    return EventSourceResponse(event_generator())
