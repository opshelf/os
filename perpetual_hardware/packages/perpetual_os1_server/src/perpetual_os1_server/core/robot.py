from typing import Optional
from perpetual_os1 import robot as r
from perpetual_os1 import common
from perpetual_os1 import robot_worker as r_worker
from perpetual_os1 import robot_client as r_client
from perpetual_os1.common import (
    ConfigError,
    ConfigValidationError,
    ConfigMissingError,
    assert_config,
)

from perpetual_os1_server.settings import get_settings, get_root_config
from perpetual_os1_server.core.robot_events import RobotEventListener
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.session import AsyncSessionLocal

import logging

logger = logging.getLogger(__name__)

robot_worker: Optional[r_worker.RobotWorker] = None
event_listener: Optional[RobotEventListener] = None


def validate_robot_config() -> None:
    try:
        config = get_root_config()
        assert_config(config, requirements=[
            "robot.tool.parameters.IDLE_POSITION",
            "robot.tool.parameters.PICKUP_POSITION_L",
            "robot.tool.parameters.PICKUP_POSITION_H",
        ])
        logger.info("Robot config validated successfully")
    except ConfigMissingError as e:
        logger.error("Config file not found: %s", e.path)
        raise
    except ConfigValidationError as e:
        logger.error("Config validation failed:\n%s", e)
        raise
    except ConfigError as e:
        logger.error("Config assertion failed: %s", e)
        raise


async def init_robot_worker() -> None:
    global robot_worker, event_listener

    logger.info("Validating robot configuration")
    validate_robot_config()

    logger.info("Getting Robot")
    settings = get_settings()
    robot = r.Robot()
    await robot.configure()
    logger.info("Built Robot")

    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        occupied = await storage_service.get_all_occupied_cells()
        occupied_tuples = [(side, shelf_idx, cell_idx) for side, shelf_idx, cell_idx, _ in occupied]
        robot.sync_occupied_cells(occupied_tuples)
        logger.info("Synced %d occupied cells from database", len(occupied_tuples))
    nats_config = common.RobotNatsConfiguration(
        domain=settings.nats.domain,
        port=settings.nats.port,
    )
    robot_worker = r_worker.RobotWorker(robot, nats_config)
    logger.info("Got Instance")
    await robot_worker.connect()
    logger.info("Connected to NATS")
    await robot_worker.start()
    logger.info("Worker started")

    event_listener = RobotEventListener(nats_config)
    await event_listener.start()
    logger.info("Robot event listener started")


async def stop_robot_worker() -> None:
    global robot_worker, event_listener
    if event_listener is not None:
        await event_listener.stop()
        logger.info("Robot event listener stopped")
    if robot_worker is not None:
        await robot_worker.shutdown()


async def get_robot_client() -> r_client.RobotClient:
    if robot_worker:
        return robot_worker.get_client()
    raise ValueError("Robot Worker was Not Initialized")


async def get_robot_worker() -> r_worker.RobotWorker:
    if robot_worker:
        return robot_worker
    raise ValueError("Robot Worker was Not Initialized")
