from perpetual_os1.robot import Robot
from perpetual_os1.gantry import GantryCoordinate
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, StorageModuleCellLocation, StorageModuleSide
from perpetual_os1.configuration import robot_configuration, storage_grid
import asyncio

tray_1 = GantryCoordinate(x=240, y=394)

tray_2 = GantryCoordinate(x=365, y=394)

tray_3 = GantryCoordinate(x=540, y=394)


async def pick_up_item(robot: Robot, coordinate: GantryCoordinate, width: float) -> None:
    await robot.grab_box(coordinate)
    await robot.drop_off_item()
    await robot.place_box(coordinate)


async def drop_off_item(robot: Robot, coordinate: GantryCoordinate, width: float) -> None:
    await robot.grab_box(coordinate)
    await robot.pickup_item()
    await robot.place_box(coordinate)



async def main() -> None:
    can_bus = await get_can_bus()
    modbus = await get_modbus_client()
    robot = Robot(can_bus, modbus, robot_configuration)
    await robot.initialize()
    robot.set_storage(storage_grid)
    #await robot.home()
    await robot.store_item(StorageModuleCellLocation(
        side=StorageModuleSide.RIGHT,
        shelf_idx=0,
        cell_idx=0
    ))
    await robot.retrieve_item(StorageModuleCellLocation(
        side=StorageModuleSide.RIGHT,
        shelf_idx=0,
        cell_idx=0
    ))
    #await robot._door.open_door()
    #await robot.move(GantryCoordinate(x=10, y=489))

    #await robot.unsafe_move(GantryCoordinate(x=271.5, y=680))
    #await robot.unsafe_move(GantryCoordinate(x=246.5, y=885))
    #await robot.tool.extend()

    #await robot._grab_cell(GantryCoordinate(x=249, y=885), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))
    #await robot._place_cell(GantryCoordinate(x=249, y=885), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))

    #for i in range(0, 8):
    #    await robot._grab_cell(GantryCoordinate(x=249+i*61, y=885), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))
    #    await robot._place_cell(GantryCoordinate(x=249+i*61, y=885), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))

    #for i in range(0, 4):
    #    await robot._grab_cell(GantryCoordinate(x=271.5+i*111, y=680), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=100, width=100, height=10)))
    #    await robot._place_cell(GantryCoordinate(x=271.5+i*111, y=680), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=100, width=100, height=100)))

    #for i in range(0, 5):
    #    await robot._grab_cell(GantryCoordinate(x=293+156*i, y=410), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=150, width=150, height=100)))
    #    await robot._place_cell(GantryCoordinate(x=293+156*i, y=410), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=150, width=150, height=100)))

    #await robot.tool.z_axis.home()
    #await robot.tool.z_axis.move_to_position(200)
    #try:
    #    await robot.tool.z_axis.wait_for_move()
    #except Exception as e:
    #    print(e)
    #print(await robot.tool.z_axis.get_position())
    #await robot.tool.z_axis.home()

    #await robot.tool.extend()
    #await robot.extend_stage()


if __name__ == "__main__":
    asyncio.run(main())
