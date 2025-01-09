from nats.aio.client import Client as NATS
from perpetual_p0c_server.core.events import LabelPrintEvent
from fastapi import APIRouter
from perpetual_p0c_server.routers.common import SuccessResponse

router = APIRouter()


@router.post("/print")
async def print_label(payload: LabelPrintEvent) -> SuccessResponse:
    nc = NATS()
    await nc.connect(servers=["nats://localhost:4222"])
    await nc.publish("printer_queue", payload.model_dump_json().encode())
    return SuccessResponse()
