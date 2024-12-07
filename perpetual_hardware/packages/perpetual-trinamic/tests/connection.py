from can import Bus
from hardware_common.stepper_motor import (
    StepperMotorConfig,
    Rotation,
)
import time
from perpetual_trinamic.trinamic_axis import TrinamicAxis


def test_axis():
    with Bus(interface="pcan", channel="PCAN_USBBUS1", bitrate=1000000, fd=False) as bus:
        axis1 = TrinamicAxis(
            bus,
            100,
            0,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=3200,
                encoder_ppr=16384,
                steps_per_mm=1600,
                work_current=1400,
                holding_current=1000,
                homing_current=500,
                velocity=50,
                acceleration=200,
                max_velocity=100,
                max_acceleration=100,
                min_travel=0,
                max_travel=185,
                invert_coordinates=False,
                home_direction=Rotation.CCW,
                homing_speed=50,
            ),
        )
        time.sleep(1)
        axis1.move_to_position(100)


if __name__ == "__main__":
    test_axis()
