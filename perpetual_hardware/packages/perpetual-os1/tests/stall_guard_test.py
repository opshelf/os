from perpetual_os1.robot import Robot
from perpetual_os1.storage import (
    StorageModuleCellLocation,
    StorageModuleSide,
    StorageGrid,
    Shelf,
    Cell,
    CellStatus,
    Dimensions,
    CalibrationCoordinate,
    GantryCoordinate,
)
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.configuration import robot_configuration
import asyncio


storage_grid = StorageGrid(
    left_module=[
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=199, y=394, z=0),
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
                    calibration_point=CalibrationCoordinate(x=178, y=870, z=0),
                    dimensions=Dimensions(length=100, height=100, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=100, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=100, width=100),
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
                    calibration_point=CalibrationCoordinate(x=152, y=1148, z=0),
                    dimensions=Dimensions(length=50, height=50, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=50, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=50, width=50),
                ),
            ],
        ),
    ],
    right_module=[],
)


async def main() -> None:
    can_bus = await get_can_bus()
    modbus = await get_modbus_client()
    robot = Robot(can_bus, modbus, robot_configuration)
    robot.set_storage(storage_grid)
    await robot.initialize()
    await robot.gantry.y_axis.init_brake_move()
    await robot.gantry.y_axis.set_acceleration(20)
    await robot.gantry.y_axis.move_to_position(1000, override_brake=True)
    position_reached = await robot.gantry.y_axis.get_position_reached()
    while not position_reached:
        load = await robot.gantry.y_axis.get_axis_parameter(206)
        print(load)
        position_reached = await robot.gantry.y_axis.get_position_reached()
        # await asyncio.sleep(0.1)
    await robot.gantry.y_axis.end_brake_move()
    await robot.gantry.y_axis.init_brake_move()
    await robot.gantry.y_axis.move_to_position(0, override_brake=True)
    position_reached = await robot.gantry.y_axis.get_position_reached()
    while not position_reached:
        load = await robot.gantry.y_axis.get_axis_parameter(206)
        print(load)
        position_reached = await robot.gantry.y_axis.get_position_reached()
        # await asyncio.sleep(0.1)
    await robot.gantry.y_axis.end_brake_move()


if __name__ == "__main__":
    asyncio.run(main())
