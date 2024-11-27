from perpetual_p0c import funnel
from perpetual_p0c.configuration import p0_funnel_config
from perpetual_p0c.utils import get_usb_serial_can
from can import Bus

if __name__ == '__main__':
    #usb_can = get_usb_serial_can()[0]
    #with Bus(interface=usb_can.CAN_INTERFACE, channel=usb_can.CAN_CHANNEL, bitrate=usb_can.CAN_BITRATE, fd=False) as bus:
    with Bus(interface="waveshare", channel="/dev/ttyUSB0", bitrate=1000000, fd=False) as bus:
        funnel = funnel.Funnel(bus, p0_funnel_config)
        funnel.home()

