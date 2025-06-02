import asyncio
from perpetual_os1.utils import get_can_bus
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
import time


configuration = CanAxisConfiguration(
    can_id=111,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=266.667,
        work_current=2000,
        holding_current=0,
        homing_current=500,
        velocity=50,
        acceleration=50,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CW,
        homing_speed=50,
        external_encoder=True,
        brake=True
    ),
    name="stage",
    axis_type="leadscrew",
)


async def main() -> None:
    can_bus = await get_can_bus()
    stage = TrinamicAxis(
        can_bus,
        configuration=configuration.configuration,
        module_id=configuration.can_id,
    )
    await stage.initialize()
    await stage.set_pullup_resistors(0)
    await stage.stop()
    input()
    #await stage.move_to_relative_position(1000)
    #await stage.wait_for_move()
    await stage.home()
    #await stage.safety_off()
    #for i in range(0, 100):
    #    await stage.init_brake_move()
    #    input()
    #    await stage.end_brake_move()
    #    input()
    ##await stage.move_relative(3200*-10)
    #await stage.end_brake_move()


if __name__ == "__main__":
    asyncio.run(main())
