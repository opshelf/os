from abc import ABCMeta, abstractmethod
from typing import Optional, Literal
from pydantic import BaseModel, Field
from enum import Enum
from nats.aio.client import Client as NATS
from typing import Optional, List, Tuple
import asyncio
import datetime
from perpetual_scale.core import AbstractScale


class ScaleReadPayload(BaseModel):
    timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now())
    weight: float


class ScaleReadEvent(BaseModel):
    kind: Literal["scale_read"] = "scale_read"
    payload: ScaleReadPayload


class ScaleWorker:
    def __init__(
        self,
        scale: AbstractScale,
        nats_url: str,
        scale_event_queue: str,
    ) -> None:
        self._nats_url = nats_url
        self._scale = scale
        self._scale_events = scale_event_queue
        self._nc = NATS()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])
        await self._scale.initialize()

    async def run(self) -> None:
        while self._running:
            try:
                result = await self._scale.read()
                if result is not None:
                    await self._nc.publish(
                        self._scale_events,
                        ScaleReadEvent(payload=ScaleReadPayload(weight=result))
                        .model_dump_json()
                        .encode(),
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(e)

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None

    async def close(self) -> None:
        await self._nc.close()
        await self._scale.shutdown()
