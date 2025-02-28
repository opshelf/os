from nats.aio.msg import Msg
from nats.aio.client import Client as NATS
from typing import Deque, Optional, Literal
from nats.aio.subscription import Subscription
import asyncio
import numpy as np
from pydantic import ValidationError
from perpetual_camera.detection import Detector
from perpetual_camera.async_reader import AsyncCameraReader
from collections import deque
from pydantic import BaseModel
import logging
import time

class GS1Raw(BaseModel):
    gs1: str

class LabelScannedEventPayload(BaseModel):
    gs1: str
    location: Optional[str] = None

class LabelScannedEvent(BaseModel):
    kind: Literal["label_scanned"] = "label_scanned"
    payload: LabelScannedEventPayload


class ItemPresenceEvent(BaseModel):
    kind: Literal["input_item_detected"] = "input_item_detected"
    payload: GS1Raw

class CameraWorker:
    def __init__(
        self,
        camera: AsyncCameraReader,
        nats_url: str,
        camera_events_queue: str = "input_box",
    ) -> None:
        self._nats_url = nats_url
        self._camera = camera
        self._camera_events_queue = camera_events_queue
        self._nc = NATS()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._detector = Detector()
        self._current_frame: Optional[np.ndarray] = None
        self._baseline_image = self._detector.get_baseline_image()
        self._similarities: Deque[float] = deque(maxlen=30) 
        self._item_present: bool = False
        self._expecting_input = False
        self._gs1: Optional[GS1Raw] = None
        self._sub: Optional[Subscription] = None
        self._scan_received_time = time.time()
        self._camera.add_callback(self.receive_camera_frame)


    async def scan_detected_handler(self, msg: Msg) -> None:
        try:
            logging.info("Event Received")
            evt = LabelScannedEvent.model_validate_json(msg.data.decode())
            if evt.payload.location == "input":
                self._gs1 = GS1Raw(gs1=evt.payload.gs1)
                self._scan_received_time = time.time()
                logging.info(f"{self._gs1}")
        except ValidationError as e:
            print(e)


    def receive_camera_frame(self, frame: np.ndarray) -> None:
        self._current_frame = frame
        is_present, similarity, _ = self._detector.item_present(self._baseline_image, frame)
        self._similarities.append(similarity)
        self._item_present = is_present


    def stable_images(self) -> bool:
        min_sim = min(self._similarities)
        max_sim = max(self._similarities)
        if max_sim - min_sim > 5:
            return False
        else:
            return True

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])
        self._sub = await self._nc.subscribe("scanner_events", cb=self.scan_detected_handler)
        self._camera.start()

    async def run(self) -> None:
        while self._running:
            try:
                if self._expecting_input and self._item_present and self.stable_images() and self._gs1:
                    logging.info("Item Present Sent")
                    await self._nc.publish(
                        self._camera_events_queue,
                        ItemPresenceEvent(
                            payload=self._gs1
                        )
                        .model_dump_json()
                        .encode(),
                    )
                    self._expecting_input = False
                    self._gs1 = None
                    if time.time() - self._scan_received_time > 10:
                        self._expecting_input = False
                        self._gs1 = None
                await asyncio.sleep(.1)
            except asyncio.CancelledError:
                pass

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._camera.start()
            self._worker_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None

    async def close(self) -> None:
        await self._nc.close()
        if not self._sub is None:
            await self._sub.unsubscribe()
