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
    #await robot.tool.home()
    #await robot.tool.gripper.home()
    #await robot.tool.setup_box_pick(100)
    #await robot.tool.setup_box_grab(100)
    #await robot.tool.setup_box_grab(100)
    #await robot.tool.z_axis.home()
    #await robot.tool.z_axis.move_to_position(160.0)
    #await robot.home()
    #await robot.move(tray_1)
    #await robot.move_to_pickup_location()
    #await robot.idle()
    #await robot.place_box(tray_1, 100)

    #await robot.grab_box(tray_1, 100)
    #await robot.drop_off_item(100)
    #await robot.place_box(tray_1, 100)
    ##await robot.idle()
    #await robot.grab_box(tray_1, 100)
    #await robot.pickup_item(100)
    #await robot.place_box(tray_1, 100)

    #await robot.idle()
    #await pick_up_item(robot, tray_1, 50)
    #await drop_off_item(robot, tray_1, 50)


    await robot.idle()
    await pick_up_item(robot, tray_2, 100)
    await drop_off_item(robot, tray_2, 100)

    #await robot.idle()
    #await pick_up_item(robot, tray_3, 145)
    #await drop_off_item(robot, tray_3, 145)

    #await robot.idle()
    #await robot.tool.gripper.home()
    #await robot.tool.gripper.axis.safety_off()
    #await robot.tool.gripper.move_relative(-1000)
    #await robot.tool.gripper.set_jaw_size(50)
    #await robot.tool.gripper.set_jaw_size(100)
    #await robot.tool.gripper.set_jaw_size(100)


if __name__ == '__main__':
    asyncio.run(main())
