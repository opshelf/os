from fastapi import APIRouter, Depends
from perpetual_p0c.robot import RobotCommands
from perpetual_p0c_server.core.robot.base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c_server.routers.common import SuccessResponse
from perpetual_p0c_server.core.robot import get_robot_client, get_robot_worker
from typing import List, Union
from perpetual_p0c_server.core.events import  StorageEvents


RobotEvents = Union[RobotCommands, StorageEvents]

router = APIRouter()


@router.post("/commands")
async def robot_commands(
    command: RobotCommands, client: AbstractRobotClient = Depends(get_robot_client)
) -> SuccessResponse:
    await client.send_command(command)
    return SuccessResponse()

@router.post("/events")
async def robot_events(
    command: StorageEvents, client: AbstractRobotClient = Depends(get_robot_client)
) -> SuccessResponse:
    await client.send_command(command)
    return SuccessResponse()


@router.post("/start")
async def start_robot(worker: AbstractRobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.start()
    return SuccessResponse()

@router.post("/stop")
async def stop_robot(worker: AbstractRobotWorker = Depends(get_robot_worker)) -> SuccessResponse:
    await worker.shutdown()
    return SuccessResponse()


@router.post("/clear_commands")
async def clear_robot_commands(
    worker: AbstractRobotWorker = Depends(get_robot_worker),
) -> SuccessResponse:
    await worker.clear_commands()
    return SuccessResponse()


@router.get("/commands")
async def commands(client: AbstractRobotClient = Depends(get_robot_client)) -> List[RobotCommands]:
    commands = await client.get_queued_commands()
    return commands
