"""
self._carriage = TrinamicAxis(
    bus,
    module_id=tool_configuration.carriage.can_id,
    motor_id=0,
    configuration=tool_configuration.carriage.configuration,
)
"""
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_p0c.utils import get_can_bus
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    axis = TrinamicAxis(
        can_bus,
        StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=3200,
            steps_per_mm=320.0,
            work_current=2000,
            holding_current=0,
            homing_current=750,
            velocity=300,
            acceleration=150,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            homing_speed=50,
        ),
        module_id=102,
    )
    await axis.initialize()
    await axis.home()
    TRAY_PICKUP_LOCATION = 93.0
    BACKOFF_DISTANCE = 50.0
    while True:
        delay_length = input("Input the Desired Delay in Milliseconds: ")
        delay = int(delay_length)
        print("Moving to Pickup")
        await axis.move_to_position(TRAY_PICKUP_LOCATION)
        await axis.wait_for_move()
        print("Activated")
        await axis.set_io(0, 1, 2)
        print("Backing Off After Pickup")
        await axis.move_to_position(TRAY_PICKUP_LOCATION-BACKOFF_DISTANCE)
        await axis.wait_for_move()
        print("Returning After Pickup")
        await axis.move_to_position(TRAY_PICKUP_LOCATION)
        await axis.wait_for_move()
        print("Deactivated")
        await axis.set_io(0, 0, 2)
        await asyncio.sleep(delay/100.0)
        print("Backoff")
        await axis.move_to_position(TRAY_PICKUP_LOCATION-BACKOFF_DISTANCE)
        await axis.wait_for_move()
        print("Done")

        """
        Home the Motor
        Move the motor 93 mm toward the tray
        Activate the Electromagnet
        Move the motor 50 mm backwards
        Move the motor motor 50 mm forward
        Deactivate the electromagnet
        Time delay
        Program should prompt the user for length of time delay before starting the script
        Move the motor back 50 mm
        10 mm of travel per revolution
        """



if __name__ == "__main__":
    asyncio.run(main())

