import time
from enum import Enum
from perpetual_p0c.output_box import OutputBox, OutputBoxOffsets, OutputBoxConfiguration

#class TestState(Enum):
#    INIITIALIZE = 0
#    OPEN_DOOR = 1
#    START_BELT = 2
#    CLOSE_DOOR = 3
#    STOP_BELT = 4
"""
p0_funnel_config = FunnelConfiguration(
    inlet_funnel=CanAxisConfiguration(
        can_id=CanMessageIds.INPUT_FUNNEL,
        configuration=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=3200,
            steps_per_mm=266.66666,
            work_current=2000,
            holding_current=SLOT_HOLDING_CURRENT,
            homing_current=1500,
            velocity=100,
            acceleration=100,
            # velocity=100,
            # acceleration=100,
            max_velocity=200,
            max_acceleration=200,
            min_travel=0,
            max_travel=0,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            # home_direction=Rotation.CW,
            homing_speed=50,
        ),
        name="inlet_funnel",
        axis_type="leadscrew",
    ),
"""

if __name__ == '__main__':
    usb_can = get_usb_serial_can()[0]
    with Bus(interface=usb_can.CAN_INTERFACE, channel=usb_can.CAN_CHANNEL, bitrate=usb_can.CAN_BITRATE, fd=False) as bus:

        


