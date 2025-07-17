from perpetual_os1.robot import Robot
from perpetual_os1.gantry import GantryCoordinate
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, StorageModuleCellLocation, StorageModuleSide
from perpetual_os1.configuration import robot_configuration, storage_grid
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    modbus = await get_modbus_client()
    robot = Robot(can_bus, modbus, robot_configuration)
    await robot.initialize()
    print(await robot.status())
    #await robot.home()
    print(await robot.size_item())

if __name__ == "__main__":
    asyncio.run(main())
