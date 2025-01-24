from fastapi import APIRouter
import asyncio
from typing import AsyncGenerator, Optional
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from sse_starlette.sse import EventSourceResponse
from nats.aio.client import Client as NATS


router = APIRouter()


async def nats_consumer(stop_event: asyncio.Event) -> AsyncGenerator[str, None]:
    nats_client = NATS()
    message_queue = asyncio.Queue[str]()

    async def message_handler(msg: Msg) -> None:
        await message_queue.put(msg.data.decode())
    
    sub: Optional[Subscription] = None
    try:
        await nats_client.connect("nats://localhost:4222")
        sub = await nats_client.subscribe("scanner", cb=message_handler)

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


@router.get("/scans")
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
