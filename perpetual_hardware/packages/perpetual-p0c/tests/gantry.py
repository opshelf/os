from perpetual_p0c.gantry import Gantry
from perpetual_p0c.configuration import p0_gantry_config
from can import Bus
import time


def test_axis():
    with Bus(interface="pcan", channel="PCAN_USBBUS1", bitrate=1000000, fd=False) as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        gantry.home()

if __name__ == '__main__':
    test_axis()


