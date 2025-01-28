"""
-Move motor clockwise to home
-Move motor X revolutions counterclockwise
-Move motor X revolutions clockwise
Controllable Parameters: Acceleration of the motor, Velocity of the motor, revolutions.
"""
import asyncio
from perpetual_trinamic.serial.connection import TrinamicUsbConnection
from perpetual_trinamic.serial.trinamic_stepper import TrinamicStepper
from perpetual_p0c.utils import get_trinamic_usb
from hardware_common.stepper_motor import StepperMotorConfig, Rotation

async def main() -> None:
    conn = get_trinamic_usb()
    connection = await TrinamicUsbConnection.build(conn.device, 115200, asyncio.get_event_loop())
    stepper = TrinamicStepper(
        connection,
        configuration=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200*20,
            encoder_ppr=3200,
            steps_per_mm=400.0,
            work_current=3000,
            holding_current=0,
            homing_current=1500,
            velocity=100,
            acceleration=100,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CW,
            homing_speed=100,
        ),
        module_id=1,
        motor_id=0
    )
    velocity = 100.0 # Revolutions Per Second
    acceleration = 100.0 # Revolutions Per Second Squared
    revolutions = 1.0
    while True:
        print(f"CURRENT SPEED: {velocity} CURRENT ACCELERATION: {acceleration} CURRENT REVOLUTIONS: {revolutions}\n")
        new_settings = input("Press Enter to Run Again. Set Velocty with V:{NEW SPEED} A:{NEW ACCELERATION} R:{NEW REVOLUTIONS} not all are necessary\n")
        items = new_settings.strip().split(" ")
        for item in items:
            if item != "" or item != " ":
                setting = item.split(":")
                if len(setting) == 2:
                    number = float(setting[1])
                    if setting[0] == "A":
                        acceleration = number
                    elif setting[0] == "V":
                        velocity = number
                    elif setting[0] == "R":
                        revolutions = number
                    else:
                        raise Exception(f"Unrecognized Identifier: {setting[0]}")
        print(f"RUNNING WITH SPEED:{velocity} ACCELERATION:{acceleration} REVOLUTIONS:{revolutions}")

        print("Homing")
        await stepper.home()
        print("Moving Counter Clockwise")
        await stepper.move_revolutions(-revolutions)
        await stepper.wait_for_move()
        print("Moving Clockwise")
        await stepper.move_revolutions(revolutions)
        await stepper.wait_for_move()
        print("Done\n")

if __name__ == '__main__':
    asyncio.run(main())
