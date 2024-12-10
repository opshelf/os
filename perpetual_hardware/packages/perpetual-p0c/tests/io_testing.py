from perpetual_p0c import funnel
from perpetual_p0c.configuration import p0_tool_config
from perpetual_p0c.utils import get_can_devices
#from perpetual_p0c.io_controller import GenericRelayBoardMessage, ToolRelayController
from perpetual_p0c.io_board import ActuatorState, IoBoard, Wedge
from can import Bus
import time

if __name__ == "__main__":
    can_bus = get_can_devices()
    with Bus(
        interface=can_bus.CAN_INTERFACE,
        channel=can_bus.CAN_CHANNEL,
        bitrate=1000000,
        fd=False,
    ) as bus:
        #tool = ToolRelayController(bus)
        #print(tool.set_relays(GenericRelayBoardMessage()))

        io = IoBoard(bus)
        io.reset_valves()
        while True:
            state = io.get_state()
            print(state)
            input()
            io.set_wedges(Wedge(wedge_1=True))
            state = io.get_state()
            print(state)
            input()
            io.set_wedges(Wedge(wedge_2=True))
            state = io.get_state()
            print(state)
            input()
            io.set_wedges(Wedge(wedge_3=True))
            state = io.get_state()
            print(state)
            input()
            io.set_actuators(ActuatorState.Extended)
            state = io.get_state()
            print(state)
            input()
            io.set_actuators(ActuatorState.Retracted)
            state = io.get_state()
            print(state)
            input()
            io.reset_valves()
            state = io.get_state()
            print(state)
            input()
        
