from fastapi import APIRouter
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1.gantry import GantryCommands

router = APIRouter()


@router.post("/gantry")
async def gantry(command: GantryCommands) -> SuccessResponse:
    print(command)
    return SuccessResponse()
