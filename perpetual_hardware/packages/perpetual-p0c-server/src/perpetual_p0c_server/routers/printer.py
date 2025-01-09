from nats.aio.client import Client as NATS
from perpetual_p0c_server.core.events import LabelPrintEvent
import json

from fastapi import APIRouter
from perpetual_p0c_server.routers.common import SuccessResponse

router = APIRouter()


@router.post("/print")
async def funnel(payload: LabelPrintEvent) -> SuccessResponse:
    nc = NATS()
    await nc.connect(servers=["nats://localhost:4222"])
    result = await nc.publish("printer_queue", payload.model_dump_json().encode())
    print(result)
    return SuccessResponse()
