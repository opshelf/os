from fastapi import APIRouter
from .common import SuccessResponse
from perpetual_p0c.gantry import GantryCommands

router = APIRouter()


@router.post("/gantry")
async def gantry(command: GantryCommands) -> SuccessResponse:
    print(command)
    return SuccessResponse()


