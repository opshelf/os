import asyncio
from perpetual_os1.utils import get_can_bus
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware.stepper_motor import StepperMotorConfig, Rotation
from perpetual_os1.gantry import GantryConfiguration, GantryCoordinate, Gantry
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_os1.configuration import (
    gantry_configuration,
    z_axis_configuration,
    gripper_axis_configuration,
)


async def set_jaw_size(axis: TrinamicAxis, size: float) -> None:
    gap = 17
    target_size = size - gap
    jaw_position = target_size / 2
    await axis.move_to_position(jaw_position)
    await axis.wait_for_move()


async def close_jaw(axis: TrinamicAxis) -> None:
    try:
        await axis.move_to_position(0)
        await axis.wait_for_move()
    except Exception as e:
        await axis.initialize()


tray_1 = GantryCoordinate(x=365, y=396)
tray_2 = GantryCoordinate(x=365, y=396)

place_offset = GantryCoordinate(x=0, y=5)


async def pick_test(gantry: Gantry, gripper: TrinamicAxis, z_axis: TrinamicAxis) -> None:
    await gantry.initialize()
    await gripper.initialize()
    await z_axis.initialize()

    await z_axis.home()
    # await gripper.home()
    # await gantry.home()
    await gantry.move_coordinate(tray_1)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 98)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()
    # await gantry.move_coordinate(tray_1+place_offset)
    # await z_axis.move_to_position(107)
    # await z_axis.wait_for_move()
    # await set_jaw_size(gripper, 102)
    # await z_axis.move_to_position(0)
    # await z_axis.wait_for_move()

    await gantry.move_coordinate(tray_1 + GantryCoordinate(x=1000, y=0))

    await gantry.move_coordinate(tray_1 + place_offset)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 102)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()

    # await gantry.move_coordinate(tray_1)
    # await z_axis.move_to_position(107)
    # await z_axis.wait_for_move()
    # await set_jaw_size(gripper, 98)
    # await z_axis.move_to_position(0)
    # await z_axis.wait_for_move()
    # await gantry.move_coordinate(tray_1+place_offset)
    # await z_axis.move_to_position(107)
    # await z_axis.wait_for_move()
    # await set_jaw_size(gripper, 102)
    # await z_axis.move_to_position(0)
    # await z_axis.wait_for_move()


async def pull_test(gantry: Gantry, gripper: TrinamicAxis, z_axis: TrinamicAxis) -> None:
    await gantry.initialize()
    await gripper.initialize()
    await z_axis.initialize()
    # await z_axis.home()
    # await gripper.home()
    # await gantry.home()

    await set_jaw_size(gripper, 106)
    await gantry.move_coordinate(tray_1)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 98)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()
    await gantry.move_coordinate(tray_1 + GantryCoordinate(x=175, y=200))
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await gantry.move_coordinate(tray_1 + GantryCoordinate(x=175, y=0))
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()

    await gantry.move_coordinate(tray_1 + place_offset)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 106)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()


async def main() -> None:
    can_bus = await get_can_bus()
    gantry = Gantry(
        can_bus,
        gantry_configuration,
    )
    gripper = TrinamicAxis(
        can_bus,
        configuration=gripper_axis_configuration.configuration,
        module_id=gripper_axis_configuration.can_id,
    )
    z_axis = TrinamicAxis(
        can_bus,
        configuration=z_axis_configuration.configuration,
        module_id=z_axis_configuration.can_id,
    )
    await gantry.move_coordinate(GantryCoordinate(x=10, y=10))
    await pick_test(gantry, gripper, z_axis)
    await pull_test(gantry, gripper, z_axis)
    """
    await gantry.initialize()
    await gripper.initialize()
    await z_axis.initialize()

    await z_axis.home()
    #await gripper.home()
    #await gantry.home()
    await gantry.move_coordinate(tray_1)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 98)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()
    #await gantry.move_coordinate(tray_1+place_offset)
    #await z_axis.move_to_position(107)
    #await z_axis.wait_for_move()
    #await set_jaw_size(gripper, 102)
    #await z_axis.move_to_position(0)
    #await z_axis.wait_for_move()

    await gantry.move_coordinate(tray_1+GantryCoordinate(x=1000, y=0))

    await gantry.move_coordinate(tray_1+place_offset)
    await z_axis.move_to_position(107)
    await z_axis.wait_for_move()
    await set_jaw_size(gripper, 102)
    await z_axis.move_to_position(0)
    await z_axis.wait_for_move()

    #await gantry.move_coordinate(tray_1)
    #await z_axis.move_to_position(107)
    #await z_axis.wait_for_move()
    #await set_jaw_size(gripper, 98)
    #await z_axis.move_to_position(0)
    #await z_axis.wait_for_move()
    #await gantry.move_coordinate(tray_1+place_offset)
    #await z_axis.move_to_position(107)
    #await z_axis.wait_for_move()
    #await set_jaw_size(gripper, 102)
    #await z_axis.move_to_position(0)
    #await z_axis.wait_for_move()
    """


if __name__ == "__main__":
    asyncio.run(main())
