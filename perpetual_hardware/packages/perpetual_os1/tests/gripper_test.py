from perpetual_trinamic.common import HomedStatus
import asyncio
from perpetual_os1.utils import get_can_bus
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from hardware.stepper_motor import StepperMotorConfig, Rotation

# Inital Gap 17mm
# 32mm per rev
# mm per rev for a jaw: 50.2654824574
# mm per rev for both jaws: 100.530964915
# Steps Per mm for a jaw: 63.66
# Steps Per mm for both jaws: 31.83
# Max Jaw Opening 150mm
# Max Jaw Travel 133mm


gripper_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.GRIPPER.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=63.66,
        work_current=2000,
        holding_current=0,
        homing_current=3000,
        velocity=200,
        acceleration=200,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=150,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        brake=True,
        external_encoder=True,
    ),
    name="gripper",
    axis_type="leadscrew",
)

z_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.Z_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=3200,
        steps_per_mm=266.667,
        work_current=2000,
        holding_current=0,
        homing_current=3000,
        velocity=200,
        acceleration=200,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
    ),
    name="z_axis",
    axis_type="leadscrew",
)


async def set_jaw_size(axis: TrinamicAxis, size: float) -> None:
    gap = 17
    target_size = size - gap
    jaw_position = target_size / 2
    await axis.move_to_position(jaw_position)
    await axis.wait_for_move()


async def gripper_test() -> None:
    can_bus = await get_can_bus()
    gripper = TrinamicAxis(
        can_bus,
        configuration=gripper_configuration.configuration,
        module_id=gripper_configuration.can_id,
    )
    await gripper.initialize()
    await gripper.set_pullup_resistors(0)
    # await gripper.safety_off()
    await gripper.home()
    await set_jaw_size(gripper, 25.0)
    input()
    await set_jaw_size(gripper, 50.0)
    input()
    await set_jaw_size(gripper, 75.0)
    input()
    await set_jaw_size(gripper, 100.0)
    input()
    await set_jaw_size(gripper, 125.0)
    input()
    # await gripper.move_to_position(10)

    # print(await gripper.get_external_encoder_postion())
    # print(await gripper.get_actual_position())
    # print(await gripper.get_target_position())
    # await gripper.init_brake_move()
    # await asyncio.sleep(1)
    # await gripper.move_relative(-320)
    # await asyncio.sleep(1)
    # await gripper.end_brake_move()
    # print(await gripper.get_external_encoder_postion())
    # print(await gripper.get_actual_position())
    # print(await gripper.get_target_position())

    # for i in range(0, 10):
    #    print(await gripper.get_external_encoder_postion())
    #    print(await gripper.get_actual_position())
    #    print(await gripper.get_target_position())
    #    await gripper.init_brake_move()
    #    await asyncio.sleep(1)
    #    await gripper.move_relative(-320)
    #    await asyncio.sleep(1)
    #    await gripper.end_brake_move()
    #    print(await gripper.get_external_encoder_postion())
    #    print(await gripper.get_actual_position())
    #    print(await gripper.get_target_position())
    #    await gripper.init_brake_move()
    #    await asyncio.sleep(1)
    #    await gripper.move_relative(320)
    #    await asyncio.sleep(1)
    #    await gripper.end_brake_move()


async def main() -> None:
    can_bus = await get_can_bus()
    zaxis = TrinamicAxis(
        can_bus,
        configuration=z_axis_configuration.configuration,
        module_id=z_axis_configuration.can_id,
    )
    await zaxis.initialize()
    await zaxis.home()
    await zaxis.move_to_position(0)


if __name__ == "__main__":
    # asyncio.run(main())
    asyncio.run(gripper_test())
