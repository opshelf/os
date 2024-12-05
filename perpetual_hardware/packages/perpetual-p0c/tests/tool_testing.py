from perpetual_p0c import funnel
from perpetual_p0c.configuration import  p0_tool_config
from perpetual_p0c.utils import get_usb_serial_can
from perpetual_p0c.io_controller import GenericRelayBoardMessage, ToolRelayController
from can import Bus

if __name__ == '__main__':
    can_bus = get_usb_serial_can()[0]
    with Bus(interface=can_bus.CAN_INTERFACE, channel=can_bus.CAN_CHANNEL, bitrate=1000000, fd=False) as bus:
        tool = ToolRelayController(bus)
        print(tool.set_relays(GenericRelayBoardMessage()))

