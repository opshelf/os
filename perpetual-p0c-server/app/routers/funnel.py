from fastapi import APIRouter
from .common import SuccessResponse
from perpetual_p0c.funnel import FunnelCommands

router = APIRouter()


@router.post("/funnel")
async def funnel(command: FunnelCommands) -> SuccessResponse:
    print(command)
    return SuccessResponse()
