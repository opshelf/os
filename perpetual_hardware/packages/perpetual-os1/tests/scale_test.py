from perpetual_os1.robot import Robot
from perpetual_os1.gantry import GantryCoordinate
from perpetual_os1.utils import get_can_bus, get_modbus_client
from perpetual_os1.configuration import robot_configuration
import asyncio

tray_1 = GantryCoordinate(
        x=240,
        y=394
)

tray_2 = GantryCoordinate(
        x=365,
        y=394
)

tray_3 = GantryCoordinate(
        x=540,
        y=394
)


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
    await robot.home()


if __name__ == '__main__':
    asyncio.run(main())
