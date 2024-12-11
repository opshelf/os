from perpetual_p0c.gantry import Gantry
from perpetual_p0c.configuration import p0_gantry_config
from perpetual_p0c.utils import get_can_devices
from can import Bus

def test_axis() -> None:
    usb_can = get_can_devices()
    with Bus(
        interface=usb_can.CAN_INTERFACE,
        channel=usb_can.CAN_CHANNEL,
        bitrate=usb_can.CAN_BITRATE,
        fd=False,
    ) as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        gantry.x_axis.home()
        print(gantry.x_axis.get_digital_input(1, 0).value)
        input()
        gantry.y_axis.home_with_brake()
        print(gantry.x_axis.get_digital_input(1, 0).value)
        input()
        gantry.move(x=100, y=100)
        input()


if __name__ == "__main__":
    test_axis()
