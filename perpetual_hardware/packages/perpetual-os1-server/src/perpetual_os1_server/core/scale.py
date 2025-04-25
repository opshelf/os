from typing import Optional
from perpetual_scale import ScaleWorker
from perpetual_scale.ruishan import SerialScale, get_scale_port
import logging

scale_worker: Optional[ScaleWorker] = None


async def init_scale_worker() -> None:
    global scale_worker
    logging.info("Initialize Scale")
    port = get_scale_port()
    scale = SerialScale(port.device, baudrate=9600)
    nats_url = "nats://localhost:4222"
    scale_event_queue = "scale.events"
    worker = ScaleWorker(scale, nats_url, scale_event_queue)
    await worker.connect()
    logging.info("Scale Connected")
    await worker.start()
    logging.info("Scale Started")
    scale_worker = worker


async def stop_scale_worker() -> None:
    global robot_worker
    if scale_worker:
        await scale_worker.close()


async def get_scale_worker() -> ScaleWorker:
    if scale_worker:
        return scale_worker
    raise ValueError("Scale Worker was Not Initialized")
