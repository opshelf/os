from perpetual_os1.door import Door, DoorConfiguration
from perpetual_os1.utils import get_can_bus
#from perpetual_os1.configuration import door_configuration
import asyncio
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation

door_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.DOOR.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=400.0,
        work_current=2000,
        holding_current=0,
        homing_current=1000,
        velocity=100,
        acceleration=100,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        external_encoder=True,
        brake=True

    ),
    name="door",
    axis_type="leadscrew",
)

door_configuration = DoorConfiguration(
    axis=door_axis_configuration, open_position=155.0, close_position=1.0
)


async def main() -> None:
    can_bus = await get_can_bus()
    door = Door(can_bus, door_configuration)
    await door.initialize()
    await door.axis.set_pullup_resistors(0)
    await door.home()
    #await door.axis.init_brake_move()
    #await door.axis.safety_off()
    #await asyncio.sleep(1)
    #await door.axis.move_relative(1000)
    #input()
    ##await door.axis.move_relative(-1000)
    #await door.axis.end_brake_move()
    ##await door.open_door()


if __name__ == "__main__":
    asyncio.run(main())
