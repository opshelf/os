import logging
from typing import Optional

from openshelf_os import robot as r
import openshelf_os.robot.worker as r_worker
from openshelf_os.common import (
    ConfigError,
    ConfigMissingError,
    ConfigValidationError,
    assert_config,
)

from openshelf_os_server.core.robot_events import RobotEventListener
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_root_config

logger = logging.getLogger(__name__)

robot_worker: Optional[r_worker.RobotWorker] = None
event_listener: Optional[RobotEventListener] = None
robot_initialization_error: Optional[str] = None
robot_initialization_error_type: Optional[str] = None


def validate_robot_config() -> None:
    try:
        config = get_root_config()
        assert_config(config, requirements=[
            "robot.tool.parameters.IDLE_POSITION",
            "robot.tool.parameters.PICKUP_POSITION_L",
            "robot.tool.parameters.PICKUP_POSITION_H",
        ])
        logger.info("Robot config validated successfully")
    except ConfigMissingError as err:
        logger.error("Config file not found: %s", err.path)
        raise
    except ConfigValidationError as err:
        logger.error("Config validation failed:\n%s", err)
        raise
    except ConfigError as err:
        logger.error("Config assertion failed: %s", err)
        raise


async def init_robot_worker() -> None:
    global robot_worker, event_listener, robot_initialization_error, robot_initialization_error_type

    robot_initialization_error = None
    robot_initialization_error_type = None
    logger.info("Validating robot configuration")
    validate_robot_config()

    logger.info("Getting Robot")
    robot = r.Robot()
    await robot.configure()
    logger.info("Built Robot")

    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        occupied = await storage_service.get_all_occupied_cells()
        occupied_tuples = [(side, shelf_idx, cell_idx) for side, shelf_idx, cell_idx, _ in occupied]
        robot.sync_occupied_cells(occupied_tuples)
        logger.info("Synced %d occupied cells from database", len(occupied_tuples))

    robot_worker = r_worker.RobotWorker(robot)
    logger.info("Got Instance")
    await robot_worker.connect()
    logger.info("Robot worker connected")
    await robot_worker.start()
    logger.info("Worker started")

    event_listener = RobotEventListener(robot_worker)
    await event_listener.start()
    logger.info("Robot event listener started")


async def stop_robot_worker() -> None:
    global robot_worker, event_listener

    if event_listener is not None:
        await event_listener.stop()
        logger.info("Robot event listener stopped")

    if robot_worker is not None:
        await robot_worker.shutdown()


async def get_robot_worker() -> r_worker.RobotWorker:
    if robot_worker is not None:
        return robot_worker

    raise ValueError("Robot Worker was Not Initialized")


def set_robot_initialization_error(error: Exception) -> None:
    global robot_initialization_error, robot_initialization_error_type

    robot_initialization_error = str(error)
    robot_initialization_error_type = type(error).__name__


def get_robot_health() -> dict[str, str]:
    if robot_worker is not None:
        return {"status": "ok"}

    if robot_initialization_error is not None:
        return {
            "status": "error",
            "error": robot_initialization_error,
            "error_type": robot_initialization_error_type or "Error",
        }

    return {"status": "disabled"}
