from perpetual_p0c.gantry import Gantry, GantryCoordinate
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
        #gantry.x_axis.home()
        #print(gantry.x_axis.get_digital_input(1, 0).value)
        #gantry.x_axis.move_to_position(50)
        #gantry.x_axis.wait_for_move()
        #print(gantry.x_axis.get_digital_input(1, 0).value)
        #input()
        #gantry.y_axis.home_with_brake()
        #print(gantry.x_axis.get_digital_input(1, 0).value)
        #input()
        #gantry.move(x=100, y=500)
        #input()
        gantry.home()
        #gantry._y_axis.move_relative(100)
        #gantry.home()
        #input()
        #gantry.y_axis.move_to_position_with_brake(100)
        for i in range(0, 11):
            gantry.move_coordinate(GantryCoordinate(x=100, y=i*100))
            input()
        #gantry.y_axis.move_to_position_with_brake(100)
        #input()


if __name__ == "__main__":
    test_axis()
