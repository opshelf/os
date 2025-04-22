import asyncio
from perpetual_os1.storage import Cell, Shelf, StorageGrid, CellStatus, Dimensions, CalibrationCoordinate, StorageModuleCellLocation, StorageModuleSide
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_os1.gantry import GantryConfiguration, GantryCoordinate, Gantry
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_os1.configuration import gantry_configuration, z_axis_configuration, gripper_axis_configuration, robot_configuration
from perpetual_os1.robot import Robot

shelf_0_tray_0 = GantryCoordinate(
        x=199,
        y=394
)

shelf_1_tray_0 = GantryCoordinate(
        x=178,
        y=872
)

shelf_2_tray_0 = GantryCoordinate(
        x=152,
        y=1148
)


shelf_0_offset = GantryCoordinate(x=166, y=-1)
shelf_1_offset = GantryCoordinate(x=121, y=-1)
shelf_2_offset = GantryCoordinate(x=71, y=-1)

async def pick_shelf_1(robot: Robot, index: int = 0) -> None:
    await robot.grab_box(shelf_0_tray_0 + GantryCoordinate(x=shelf_0_offset.x*index, y=shelf_0_offset.y*index))

async def place_shelf_1(robot: Robot, index: int = 0) -> None:
    await robot.place_box(shelf_0_tray_0 + GantryCoordinate(x=shelf_0_offset.x*index, y=shelf_0_offset.y*index))

async def pick_shelf_2(robot: Robot, index: int = 0) -> None:
    await robot.grab_box(shelf_1_tray_0 + GantryCoordinate(x=shelf_1_offset.x*index, y=shelf_1_offset.y*index))

async def place_shelf_2(robot: Robot, index: int = 0) -> None:
    await robot.place_box(shelf_1_tray_0 + GantryCoordinate(x=shelf_1_offset.x*index, y=shelf_1_offset.y*index))

async def pick_shelf_3(robot: Robot, index: int = 0) -> None:
    await robot.grab_box(shelf_2_tray_0 + GantryCoordinate(x=shelf_2_offset.x*index, y=shelf_2_offset.y*index))

async def place_shelf_3(robot: Robot, index: int = 0) -> None:
    await robot.place_box(shelf_2_tray_0 + GantryCoordinate(x=shelf_2_offset.x*index, y=shelf_2_offset.y*index))


storage_grid = StorageGrid(
    left_module=[
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point= CalibrationCoordinate(
                        x=199,
                        y=394,
                        z=0
                    ),
                    dimensions=Dimensions(
                        length=145,
                        height=100,
                        width=145
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=145,
                        height=100,
                        width=145
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=145,
                        height=100,
                        width=145
                    )
                )
            ]
        ),
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point= CalibrationCoordinate(
                        x=178,
                        y=870,
                        z=0
                    ),
                    dimensions=Dimensions(
                        length=100,
                        height=100,
                        width=100
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=100,
                        height=100,
                        width=100
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=100,
                        height=100,
                        width=100
                    )
                )
            ]
        ),
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point= CalibrationCoordinate(
                        x=152,
                        y=1148,
                        z=0
                    ),
                    dimensions=Dimensions(
                        length=50,
                        height=50,
                        width=50
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=50,
                        height=50,
                        width=50
                    )
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(
                        length=50,
                        height=50,
                        width=50
                    )
                )
            ]
        )
    ],
    right_module=[]
)

async def storage_grid_test(robot: Robot) -> None:
    await robot.grab_cell(
        StorageModuleCellLocation(
        side=StorageModuleSide.LEFT,
        shelf_idx=0,
        cell_idx=0
    ))
    await robot.place_cell(
        StorageModuleCellLocation(
        side=StorageModuleSide.LEFT,
        shelf_idx=0,
        cell_idx=0
    ))



async def main() -> None:
    can_bus = await get_can_bus()
    modbus = await get_modbus_client()
    robot = Robot(can_bus, modbus, robot_configuration)
    robot.set_storage(storage_grid)
    await robot.initialize()
    #await robot.home()
    await robot.idle()
    #await robot.move_to_pickup_location()
    input()
    for i in range(0, 3):
        await robot.retrieve_item(
            StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=1,
                cell_idx=i
            )
        )
        if i == 2:
            await robot.store_item(
                StorageModuleCellLocation(
                    side=StorageModuleSide.LEFT,
                    shelf_idx=1,
                    cell_idx=0
                )
            )
        else: 
            await robot.store_item(
                StorageModuleCellLocation(
                    side=StorageModuleSide.LEFT,
                    shelf_idx=1,
                    cell_idx=i+1
                )
            )
    """
    input()
    for i in range(0, 3):
        await robot.grab_cell(
            StorageModuleCellLocation(
            side=StorageModuleSide.LEFT,
            shelf_idx=0,
            cell_idx=i
        ))
        if i == 2:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=0,
                cell_idx=0
            ))
        else:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=0,
                cell_idx=i+1
            ))
    for i in range(0, 3):
        await robot.grab_cell(
            StorageModuleCellLocation(
            side=StorageModuleSide.LEFT,
            shelf_idx=1,
            cell_idx=i
        ))
        if i == 2:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=1,
                cell_idx=0
            ))
        else:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=1,
                cell_idx=i+1
            ))
    for i in range(0, 3):
        await robot.grab_cell(
            StorageModuleCellLocation(
            side=StorageModuleSide.LEFT,
            shelf_idx=2,
            cell_idx=i
        ))
        if i == 2:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=2,
                cell_idx=0
            ))
        else:
            await robot.place_cell(
                StorageModuleCellLocation(
                side=StorageModuleSide.LEFT,
                shelf_idx=2,
                cell_idx=i+1
            ))
    """

if __name__ == "__main__":
    asyncio.run(main())

