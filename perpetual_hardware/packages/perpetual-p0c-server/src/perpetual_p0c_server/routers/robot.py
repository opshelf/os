from fastapi import APIRouter, Depends
from perpetual_p0c.robot import RobotCommands
from perpetual_p0c_server.core.robot.base import AbstractRobotClient
from perpetual_p0c_server.routers.common import SuccessResponse
from perpetual_p0c_server.core.robot import get_robot_client

router = APIRouter()


@router.post("/commands")
async def robot_commands(
    command: RobotCommands, client: AbstractRobotClient = Depends(get_robot_client)
) -> SuccessResponse:
    print(command)
    await client.send_command(command)
    return SuccessResponse()
