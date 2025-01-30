from typing import Optional
from perpetual_p0c_server.core.robot.nats import RobotWorker
from perpetual_p0c_server.core.robot.base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config
from perpetual_p0c.robot import P0C
from perpetual_p0c_server.core.robot.mock import MockRobot
from perpetual_p0c_server.core.storage import get_storage
import logging

robot_worker: Optional[AbstractRobotWorker] = None


async def init_robot_worker(enabled: bool) -> None:
    global robot_worker
    if enabled:
        logging.info("Getting Bus")
        can_bus = await get_can_bus()
        logging.info("Got Bus")
        robot = await P0C.build(can_bus, p0_robot_config)
        logging.info("Built Robot")
        robot_worker = await RobotWorker.get_instance(robot)
        logging.info("Got Instance")
        storage = get_storage()
        await robot_worker.set_storage(storage)
        await robot_worker.connect()
        logging.info("Started")
        await robot_worker.start()
        logging.info("Connected")
    else:
        mock_robot = MockRobot(p0_robot_config)
        robot_worker = await RobotWorker.get_instance(mock_robot)
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
