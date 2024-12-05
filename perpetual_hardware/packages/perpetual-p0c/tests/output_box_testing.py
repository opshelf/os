from perpetual_p0c.output_box import OutputBox, OutputBoxOffsets, OutputBoxConfiguration
from perpetual_p0c.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import (
    StepperMotorConfig,
    Rotation,
)
from perpetual_trinamic.trinamic_axis import TrinamicStepper
from perpetual_p0c.utils import get_usb_serial_can
from can import Bus

config = OutputBoxConfiguration(
    door=CanAxisConfiguration(
        can_id=CanMessageIds.OUTPUT_BOX,
        configuration=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=3200,
            steps_per_mm=266.66666,
            work_current=2000,
            holding_current=0,
            homing_current=1000,
            velocity=100,
            acceleration=100,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            homing_speed=50,
        ),
        name="output_box",
        axis_type="leadscrew",
    ),
    offsets=OutputBoxOffsets(
        door_open_position=250.0
    )
)



if __name__ == '__main__':
    usb_can = get_usb_serial_can()[0]
    with Bus(interface=usb_can.CAN_INTERFACE, channel=usb_can.CAN_CHANNEL, bitrate=usb_can.CAN_BITRATE, fd=False) as bus:
        output_box = OutputBox(bus, config)
        output_box.home()
        belt = TrinamicStepper(
            bus,
            module_id=CanMessageIds.IO_OUT_CONVEYOR,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=200,
                encoder_ppr=3200,
                steps_per_mm=266.66666,
                work_current=5000,
                holding_current=0,
                homing_current=1000,
                velocity=100,
                acceleration=100,
                max_velocity=200,
                max_acceleration=200,
                min_travel=0,
                max_travel=0,
                invert_coordinates=False,
                home_direction=Rotation.CCW,
                homing_speed=50,
            ),
            motor_id=0,
            max_current=6000,
        )
        while True:
            result = input("Press Enter to Open the Door")
            output_box.open()
            result = input("Press Enter to Start the Belt")
            belt.rotate(int(16*200*3))
            result = input("Press Enter to Stop the Belt")
            belt.stop()
            result = input("Press Enter to Close the Door")
            output_box.close()
        


        


