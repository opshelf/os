from typing import Optional
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.configuration import robot_configuration
from perpetual_os1.robot_worker import RobotWorker
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.robot import Robot
from perpetual_os1.storage import (
    Cell,
    Shelf,
    StorageGrid,
    CellStatus,
    Dimensions,
    CalibrationCoordinate,
)
import logging

robot_worker: Optional[RobotWorker] = None

storage_grid = StorageGrid(
    right_module=[
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=295, y=410, z=0),
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
            ],
        ),
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=274.5, y=680, z=0),
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
            ],
        ),
        #Shelf(
        #    shelf_height=200,
        #    shelf_width=1000,
        #    cells=[
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=CalibrationCoordinate(x=249.5, y=885, z=0),
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #        Cell(
        #            status=CellStatus.EMPTY,
        #            item=None,
        #            calibration_point=None,
        #            dimensions=Dimensions(length=50, height=200, width=50),
        #        ),
        #    ],
        #),
    ],
    left_module=[],
)

def load_storage_from_file(filename: str) -> StorageGrid:
    with open(filename) as f:
        return StorageGrid.model_validate_json(f.read())


async def init_robot_worker() -> None:
    global robot_worker
    logging.info("Getting Bus")
    can_bus = await get_can_bus()
    client = await get_modbus_client()
    logging.info("Got Bus")
    robot = Robot(can_bus, client, robot_configuration)
    storage_grid = load_storage_from_file("storage.json")
    robot.set_storage(storage_grid)
    await robot.initialize()
    logging.info("Built Robot")
    robot_worker = RobotWorker(robot, RobotNatsConfiguration())
    logging.info("Got Instance")
    await robot_worker.connect()
    logging.info("Started")
    await robot_worker.start()
    logging.info("Connected")


async def stop_robot_worker() -> None:
    global robot_worker
    if robot_worker:
        await robot_worker.shutdown()


async def get_robot_client() -> RobotClient:
    if robot_worker:
        return robot_worker.get_client()
    raise ValueError("Robot Worker was Not Initialized")


async def get_robot_worker() -> RobotWorker:
    if robot_worker:
        return robot_worker
    raise ValueError("Robot Worker was Not Initialized")
