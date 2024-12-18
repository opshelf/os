from perpetual_p0c.funnel import Funnel
from perpetual_p0c.configuration import p0_funnel_config
from can import Bus
from perpetual_p0c.utils import get_can_devices
import time
from perpetual_p0c.storage import Tray

if __name__ == "__main__":
    usb_can = get_can_devices()
    with Bus(
        interface=usb_can.CAN_INTERFACE,
        channel=usb_can.CAN_CHANNEL,
        bitrate=usb_can.CAN_BITRATE,
        fd=False,
    ) as bus:
        funnel = Funnel(bus, p0_funnel_config)
        funnel.stop()
        #funnel.home()
        #for i in range(0, 10):
        #funnel.slot_2.move_to_position(-300)
        #funnel.slot_2.wait_for_move()
        #funnel.slot_2.move_to_position(0)
        #funnel.slot_2.wait_for_move()


        #for slot in range(0, 6):
        #    funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), slot)
        #    input()
        #for slot in reversed(range(0, 6)):
        #    funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), slot)
        #    input()
        #funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), 0)
        #funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), 5)
        #funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), 0)
