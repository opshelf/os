import asyncio
from perpetual_os1.utils import get_can_bus
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_os1.gantry import GantryConfiguration, GantryCoordinate, Gantry


x_axis=CanAxisConfiguration(
        can_id=CanMessageIds.X_AXIS.value,
        configuration=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=4000,
            steps_per_mm=40,
            work_current=5000,
            holding_current=0,
            homing_current=4000,
            velocity=750,
            acceleration=500,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            homing_speed=50,
            external_encoder=True
        ),
        name="x_axis",
        axis_type="linear_axis",
)

y_axis=CanAxisConfiguration(
        can_id=CanMessageIds.Y_AXIS.value,
        configuration=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=4000,
            steps_per_mm=40,
            work_current=5000,
            holding_current=5000,
            homing_current=2000,
            velocity=150,
            acceleration=50,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            homing_speed=50,
            brake=True,
            external_encoder=True
        ),
        name="y_axis",
        axis_type="linear_axis",
    )
gantry_configuration = GantryConfiguration(x_axis=x_axis, y_axis=y_axis)

async def main() -> None:
    can_bus = await get_can_bus()
    gantry = Gantry(
        can_bus,
        gantry_configuration,
    )
    await gantry.initialize()
    await gantry.home()
    await gantry.move(x=0, y=100)
    await gantry.move(x=100, y=100)
    #await gantry.x_axis.safety_off()
    #print(await gantry.x_axis.encoder_position())
    #await gantry.x_axis.move_relative(3200)
    #await asyncio.sleep(2)
    #print(await gantry.x_axis.encoder_position())
    #await gantry.x_axis.move_relative(-3200)
    #await asyncio.sleep(2)
    #print(await gantry.x_axis.encoder_position())


    #await gantry.y_axis.safety_off()
    #await gantry.y_axis.init_brake_move()
    #print(await gantry.y_axis.encoder_position())
    #await gantry.y_axis.move_relative(3200)
    #await asyncio.sleep(2)
    #print(await gantry.x_axis.encoder_position())
    #await gantry.y_axis.move_relative(-3200)
    #await asyncio.sleep(2)
    #print(await gantry.x_axis.encoder_position())
    #await gantry.y_axis.end_brake_move()

    #await gantry.move_coordinate(GantryCoordinate(x=0, y=100))
    #await gantry.move_coordinate(GantryCoordinate(x=1000, y=100))
    #await gantry.home()
    """
    await gantry.x_axis.home()
    await gantry.x_axis.safety_off()
    print(await gantry.x_axis.get_external_encoder_postion())
    print(await gantry.x_axis.get_actual_position())
    print(await gantry.x_axis.get_target_position())
    input()
    await gantry.x_axis.move_relative(3200)
    input()
    print(await gantry.x_axis.get_external_encoder_postion())
    print(await gantry.x_axis.get_actual_position())
    print(await gantry.x_axis.get_target_position())
    await gantry.x_axis.move_relative(-3200)
    input()
    print(await gantry.x_axis.get_external_encoder_postion())
    print(await gantry.x_axis.get_actual_position())
    print(await gantry.x_axis.get_target_position())
    #await gantry.home()
    """

    #await gantry.y_axis.home()
    #await gantry.y_axis.move_to_position(100)
    #input()
    #await gantry.y_axis.stop()
    #await gantry.y_axis.init_brake_move()
    #await gantry.y_axis.set_standby_current(0)
    #input()

    #await gantry.y_axis.safety_off()
    #print(await gantry.y_axis.get_external_encoder_postion())
    #print(await gantry.y_axis.get_actual_position())
    #print(await gantry.y_axis.get_target_position())
    #await gantry.y_axis.init_brake_move()
    #input()
    #await gantry.y_axis.move_relative(3200)
    #input()
    #print(await gantry.y_axis.get_external_encoder_postion())
    #print(await gantry.y_axis.get_actual_position())
    #print(await gantry.y_axis.get_target_position())
    #await gantry.y_axis.move_relative(-3200)
    #input()
    #print(await gantry.y_axis.get_external_encoder_postion())
    #print(await gantry.y_axis.get_actual_position())
    #print(await gantry.y_axis.get_target_position())
    #await gantry.y_axis.end_brake_move()



if __name__ == "__main__":
    asyncio.run(main())
