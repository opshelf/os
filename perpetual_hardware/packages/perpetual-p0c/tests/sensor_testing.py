from perpetual_p0c.funnel import Funnel
from perpetual_p0c.workstation import Workstation
from perpetual_p0c.utils import get_bus
from perpetual_p0c.io_board import ActuatorState, IoController
from perpetual_p0c.configuration import p0_workstation_configuration, p0_funnel_config
import time
from enum import Enum


class BottleTracking(int, Enum):
    INITIALIZE = 0x00
    WAIT_FOR_TRANSFER_TRIGGER = 0x01
    START_WORKSTATION_OUTPUT = 0x02
    WAIT_FOR_FUNNEL_OUT_TRIGGER = 0x03
    WAIT_FOR_OUTPUT_TRIGGER = 0x04
    CLOSE_DOOR = 0x05
    RESET = 0x06

if __name__ == "__main__":
    can_bus = get_bus()
    with can_bus as bus:
        io = IoController(bus)
        #io.set_actuators(ActuatorState.Retracted)
        workstation = Workstation(bus, p0_workstation_configuration) 
        workstation.stop_output_belt()
        workstation.home()
        workstation.open_output()
        funnel = Funnel(bus, p0_funnel_config)
        funnel.stop()
        #funnel.home()
        #funnel.opposing_funnel.move_to_position(320)
        #input()
        ## funnel.start_output()
        ## funnel.start_output_transfer()
        ## funnel.start_belt()
        ## #input()
        ## #funnel.stop_output()
        ## #workstation.home()
        ## #workstation.open_output()
        #funnel.start_output_transfer()
        #funnel.start_output()
        ## workstation.start_output_belt()
        ## workstation.start_input_belt()
        #input()
        #funnel.stop_output()
        #funnel.stop_output_transfer()
        # #workstation.stop_output_belt()
        # #workstation.stop_input_belt()
        # #input()
        tracking = BottleTracking.INITIALIZE
        while io.get_sensors():
            pass
        input()
        while True:
            print(tracking)
            try:
                if tracking == BottleTracking.INITIALIZE:
                    funnel.start_output_transfer()
                    funnel.start_output()
                    workstation.open_output()
                    tracking = BottleTracking.WAIT_FOR_TRANSFER_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_TRANSFER_TRIGGER:
                    sensors = io.get_sensors()
                    if sensors and sensors.output_transfer_conveyor:
                        time.sleep(1)
                        funnel.stop_output_transfer()
                        tracking = BottleTracking.START_WORKSTATION_OUTPUT
                elif tracking == BottleTracking.START_WORKSTATION_OUTPUT:
                    workstation.start_output_belt()
                    tracking = BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER:
                    sensors = io.get_sensors()
                    if sensors and sensors.output_conveyor:
                        funnel.stop_output()
                        time.sleep(1)
                        tracking = BottleTracking.WAIT_FOR_OUTPUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_OUTPUT_TRIGGER:
                    sensors = io.get_sensors()
                    if sensors and sensors.output_box:
                        time.sleep(1)
                        tracking = BottleTracking.CLOSE_DOOR
                elif tracking == BottleTracking.CLOSE_DOOR:
                    workstation.close_output()
                    workstation.stop_output_belt()
                    tracking = BottleTracking.RESET
                elif tracking == BottleTracking.RESET:
                    input()
                    tracking = BottleTracking.INITIALIZE
            except Exception as e:
                print(e)
                funnel.stop()
                workstation.stop_output_belt()
                workstation.stop_input_belt()
                break
