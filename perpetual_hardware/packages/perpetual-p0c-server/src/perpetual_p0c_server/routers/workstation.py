from fastapi import APIRouter, Depends
from perpetual_p0c_server.core.workstation.worker import WorkstationClient
from perpetual_p0c_server.core.events import WorkstationEvents
from perpetual_p0c_server.routers.common import SuccessResponse
from perpetual_p0c_server.core.workstation import get_workstation_client

router = APIRouter()


@router.post("/events")
async def robot_events(
    command: WorkstationEvents, client: WorkstationClient = Depends(get_workstation_client)
) -> SuccessResponse:
    await client.send_command(command)
    return SuccessResponse()
