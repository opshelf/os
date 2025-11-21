from typing import Optional
from perpetual_os1 import robot as r
from perpetual_os1 import common
from perpetual_os1 import robot_worker as r_worker
from perpetual_os1 import robot_client as r_client

from perpetual_os1_server.core.camera import init_camera_manager
from perpetual_os1_server.core.robot_events import RobotEventListener
from perpetual_os1_server.settings import get_settings
from perpetual_os1_server.database.session import AsyncSessionLocal

import logging

robot_worker: Optional[r_worker.RobotWorker] = None
robot_event_listener: Optional[RobotEventListener] = None

async def init_robot_worker() -> None:
    global robot_worker, robot_event_listener
    logging.info("Getting Robot")
    robot = r.Robot()
    await robot.configure()
    # await init_camera_manager(robot._io._cam)
    logging.info("Built Robot")
    settings = get_settings()
    nats_config = common.RobotNatsConfiguration(
        domain=settings.nats.domain,
        port=settings.nats.port,
    )
    robot_worker = r_worker.RobotWorker(robot, nats_config)
    logging.info("Got Instance")
    await robot_worker.connect()
    logging.info("Started")
    await robot_worker.start()
    logging.info("Connected")
    try:
        robot_event_listener = RobotEventListener(nats_config)
        await robot_event_listener.start()
    except Exception as err:
        logging.warning("Failed to start robot event listener: %s", err)
        robot_event_listener = None


async def stop_robot_worker() -> None:
    global robot_worker, robot_event_listener
    if robot_worker:
        await robot_worker.shutdown()
    if robot_event_listener:
        await robot_event_listener.stop()
        robot_event_listener = None


async def get_robot_client() -> r_client.RobotClient:
    if robot_worker:
        return robot_worker.get_client()
    raise ValueError("Robot Worker was Not Initialized")


async def get_robot_worker() -> r_worker.RobotWorker:
    if robot_worker:
        return robot_worker
    raise ValueError("Robot Worker was Not Initialized")
