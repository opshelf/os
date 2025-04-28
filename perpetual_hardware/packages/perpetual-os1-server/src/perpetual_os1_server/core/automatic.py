import asyncio
from typing import Deque, Optional
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.aio.client import Client as NATS
from perpetual_barcode_scanner import LabelScannedEvent
from perpetual_os1.robot import StoreItemRequested
from perpetual_os1.storage import CellItem
from perpetual_scale import ScaleReadEvent
from perpetual_os1.robot_worker import WorkerStatus
from perpetual_os1_server.core.robot import get_robot_client
from perpetual_os1_server.core.sizes import get_size_db
from perpetual_os1_server.settings import get_settings
from collections import deque


class AutomaticStorage:
    def __init__(self) -> None:
        self._client = NATS()
        self._scale_weights: Deque[float] = deque(maxlen=10)
        self._scale_weight: Optional[float] = None
        self._stable: bool = False
        self._scan: Optional[str] = None
        self._status: Optional[WorkerStatus] = None
        self._scanner_sub: Optional[Subscription] = None
        self._scale_sub: Optional[Subscription] = None
        self._status_sub: Optional[Subscription] = None
        self._scale_threshold: float = 10.0

    async def scan_handler(self, msg: Msg) -> None:
        evt = LabelScannedEvent.model_validate_json(msg.data.decode())
        self._scan = evt.payload

    async def scale_handler(self, msg: Msg) -> None:
        evt = ScaleReadEvent.model_validate_json(msg.data.decode())
        self._scale_weight = evt.payload.weight
        self._scale_weights.append(evt.payload.weight)
        savg = sum(self._scale_weights)/len(self._scale_weights)
        smax = abs(max(self._scale_weights) - savg)
        smin = abs(min(self._scale_weights) - savg)
        peak = max(smax, smin)
        if savg > 0.0 and peak/savg < .05:
            self._stable = True
        else:
            self._stable = False

    async def status_handler(self, msg: Msg) -> None:
        self._status = WorkerStatus.model_validate_json(msg.data.decode())

    async def connect(self) -> None:
        await self._client.connect(get_settings().nats.connection_string())
        self._scanner_sub = await self._client.subscribe("scanner.events", cb=self.scan_handler)
        self._scale_sub = await self._client.subscribe("scale.events", cb=self.scale_handler)
        self._status_sub = await self._client.subscribe("robot.status", cb=self.status_handler)

    async def run(self) -> None:
        while True:
            if (
                self._scan
                and self._status
                and self._scale_weight
                and self._scale_weight > self._scale_threshold
                and self._status.robot_status
                and self._stable
            ):
                print(self._scan, self._status.robot_status.light_curtain, self._scale_weight)
                size_db = get_size_db()
                print(size_db)
                data = size_db.get_item_size(self._scan)
                if data and not self._status.robot_status.light_curtain:
                    client = await get_robot_client()
                    await client.send_command(
                        StoreItemRequested(
                            item=CellItem(
                                dimensions=data.size,
                                data=data.data,
                                weight=self._scale_weight,
                                upca=self._scan,
                            )
                        )
                    )
                    self._scan = None

                print(data)
            await asyncio.sleep(1)

    async def close(self) -> None:
        if not self._scanner_sub is None:
            await self._scanner_sub.unsubscribe()
        if not self._scale_sub is None:
            await self._scale_sub.unsubscribe()
        if not self._status_sub is None:
            await self._status_sub.unsubscribe()
        await self._client.close()
