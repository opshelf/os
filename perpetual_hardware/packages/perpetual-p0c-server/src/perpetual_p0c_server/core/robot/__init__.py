from typing import Optional
from perpetual_p0c_server.core.robot.nats import RobotWorker
from perpetual_p0c_server.core.robot.base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config
from perpetual_p0c.robot import P0C
from perpetual_p0c_server.core.robot.mock import MockRobot

robot_worker: Optional[AbstractRobotWorker] = None


async def init_robot_worker(mock: bool) -> None:
    global robot_worker
    if mock:
        mock_robot = MockRobot(p0_robot_config)
        robot_worker = await Robot_worker_workerWorker.get_instance(mock_robot)
        await robot_worker.connect()
        await robot_worker.start()
    else:
        can_bus = await get_can_bus()
        robot = await P0C.build(can_bus, p0_robot_config)
        robot_worker = await RobotWorker.get_instance(robot)
        await robot_worker.connect()
        await robot_worker.start()


async def stop_robot_worker() -> None:
    global robot_worker
    if robot_worker:
        await robot_worker.shutdown()


async def get_robot_client() -> AbstractRobotClient:
    if robot_worker:
        return await robot_worker.get_client()
    raise ValueError("Robot Worker was Not Initialized")


async def get_robot_worker() -> AbstractRobotWorker:
    if robot_worker:
        return robot_worker
    raise ValueError("Robot Worker was Not Initialized")
