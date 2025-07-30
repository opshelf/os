from fastapi import APIRouter, Depends
from perpetual_os1_server.routers.common import SuccessResponse
from perpetual_os1.gantry import GantryCommands
from .auth_deps import get_current_user, require_permissions

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.post("/gantry", dependencies=[Depends(require_permissions("gantry:control"))])
async def gantry(command: GantryCommands) -> SuccessResponse:
    print(command)
    return SuccessResponse()
