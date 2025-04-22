from perpetual_os1.utils import get_modbus_client, get_can_bus
from perpetual_os1.tool import Tool
from perpetual_os1.configuration import tool_configuration, robot_configuration
from perpetual_os1.storage import Cell, Shelf, StorageGrid, CellStatus, Dimensions, CalibrationCoordinate, StorageModuleCellLocation, StorageModuleSide
from perpetual_os1.robot import Robot
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

async def main() -> None:
    client = await get_modbus_client()
    bus = await get_can_bus()
    robot = Robot(bus, client, robot_configuration)
    await robot.initialize()
    robot.set_storage(storage_grid)
    #location = robot.get_storage().get_cell_coordinate(StorageModuleCellLocation(shelf_idx=0, cell_idx=0, side=StorageModuleSide.LEFT)).to_gantry_coordinate()
    #await robot.home()
    #await robot.tool.home()
    #await robot.move(location)
    #await robot.tool.set_jaw_size(20)
    #await robot.tool.retract_z()
    ##await robot.tool.setup_box_pick()
    ##await robot.tool.extend()
    #await robot.tool.grab_box()
    #await robot.tool.place_box()
    for i in range(0, 3):
        await robot.grab_cell(StorageModuleCellLocation(shelf_idx=0, cell_idx=i, side=StorageModuleSide.LEFT))
        if i == 2:
            await robot.place_cell(StorageModuleCellLocation(shelf_idx=0, cell_idx=0, side=StorageModuleSide.LEFT))
        else:
            await robot.place_cell(StorageModuleCellLocation(shelf_idx=0, cell_idx=i+1, side=StorageModuleSide.LEFT))
    

    #await robot.home()
    #for i in range(20, 41):
    #    await robot.tool.set_jaw_size(i)
    #    input()
    #tool = Tool(bus, client, tool_configuration) 
    #await tool.initialize()
    print("Client")

if __name__ == '__main__':
    asyncio.run(main())

