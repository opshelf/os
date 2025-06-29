from perpetual_os1.robot import Robot
from perpetual_os1.gantry import GantryCoordinate
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageModuleCellLocation, StorageModuleSide, StorageGrid
from perpetual_os1.configuration import robot_configuration
import asyncio

tray_1 = GantryCoordinate(x=355, y=1089)

TRAY_CC = 116.5
TRAY = 85.5


storage_grid = StorageGrid(
    right_module=[],
    left_module=[
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(
                        x=355,
                        y=1089,
                        z=0
                    ),
                    dimensions=Dimensions(length=127.8, height=100, width=85.5),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(
                        x=355,
                        y=1089,
                        z=0
                    ),
                    dimensions=Dimensions(length=127.8, height=100, width=85.5),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(
                        x=355,
                        y=1089,
                        z=0
                    ),
                    dimensions=Dimensions(length=127.8, height=100, width=85.5),
                )
            ]
        )
    ],
)

async def retrieve_store(robot: Robot) -> None:
    robot.set_storage(storage_grid)
    for i in range(0, 3):
        await robot.retrieve_item(
            StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=0,
                cell_idx=i
            )
        )
        await robot.idle()
        await robot.store_item(
            StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=0,
                cell_idx=i
            )
        )
        await robot.idle()

async def pick_test(robot: Robot) -> None:
    #await robot.initialize()
    robot.set_storage(storage_grid)
    #await robot.home()
    await robot.tool.rotate_to_side(StorageModuleSide.LEFT)
    for i in range(0, 3):
        await robot._grab_cell(tray_1 + GantryCoordinate(x=TRAY_CC*i, y=0), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))
        await robot._place_cell(tray_1 + GantryCoordinate(x=TRAY_CC*i, y=0), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=50, width=50, height=50)))

async def first_location(robot: Robot) -> None:
    #await robot.initialize()
    #robot.set_storage(storage_grid)
    #await robot.home()
    await robot.tool.rotate_to_side(StorageModuleSide.LEFT)
    #await robot.move(GantryCoordinate(x=355.0, y=1089))
    #await robot.move(tray_1)
    #await robot.tool.extend()
    #await robot.tool.setup_box_pick()
    #await robot.extend_stage()


async def main() -> None:
    can_bus = await get_can_bus()
    modbus = await get_modbus_client()
    robot = Robot(can_bus, modbus, robot_configuration)
    await robot.initialize()
    await robot.home()
    #print(await robot.size_item())

    #await first_location(robot)
    #await pick_test(robot)
    await retrieve_store(robot)


    #input()
    #await robot.retract_stage()
    #input()
    #await robot.home()
    #await robot.store_item(StorageModuleCellLocation(
    #    side=StorageModuleSide.RIGHT,
    #    shelf_idx=0,
    #    cell_idx=0
    #))
    #await robot.retrieve_item(StorageModuleCellLocation(
    #    side=StorageModuleSide.RIGHT,
    #    shelf_idx=0,
    #    cell_idx=0
    #))
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

    #for i in range(0, 10):
    #    await robot._grab_cell(GantryCoordinate(x=271.5, y=680), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=100, width=100, height=10)))
    #    await robot._place_cell(GantryCoordinate(x=271.5, y=680), Cell(status=CellStatus.EMPTY, item=None, calibration_point=None, dimensions=Dimensions(length=100, width=100, height=100)))

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
