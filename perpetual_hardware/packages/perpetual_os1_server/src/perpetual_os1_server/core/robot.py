from typing import Optional
from perpetual_os1 import robot as r
from perpetual_os1 import common
from perpetual_os1 import robot_worker as r_worker
from perpetual_os1 import robot_client as r_client

import logging

robot_worker: Optional[r_worker.RobotWorker] = None

async def init_robot_worker() -> None:
    global robot_worker
    logging.info("Getting Robot")
    robot = r.Robot("/dev/ttyUSB0")
    await robot.configure()
    logging.info("Built Robot")
    robot_worker = r_worker.RobotWorker(robot, common.RobotNatsConfiguration())
    logging.info("Got Instance")
    await robot_worker.connect()
    logging.info("Started")
    await robot_worker.start()
    logging.info("Connected")


async def stop_robot_worker() -> None:
    global robot_worker
    if robot_worker:
        await robot_worker.shutdown()


async def get_robot_client() -> r_client.RobotClient:
    if robot_worker:
        return robot_worker.get_client()
    raise ValueError("Robot Worker was Not Initialized")


async def get_robot_worker() -> r_worker.RobotWorker:
    if robot_worker:
        return robot_worker
    raise ValueError("Robot Worker was Not Initialized")
