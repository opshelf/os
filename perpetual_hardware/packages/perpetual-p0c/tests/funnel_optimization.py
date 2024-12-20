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
        funnel.home()
        #funnel._slot_1.home()
        ##funnel._slot_3.home()
        #funnel._slot_3.initialize()
        #
        #funnel._slot_3.safety_off()
        input()
        for i in range(0, 10):
            funnel._slot_1.move_to_position(150)
            funnel._slot_2.move_to_position(150)
            funnel._slot_3.move_to_position(150)
            funnel.wait_for_move()
            funnel._slot_1.move_to_position(0)
            funnel._slot_2.move_to_position(0)
            funnel._slot_3.move_to_position(0)
            funnel.wait_for_move()
        #for i in range(0, 10):

        #    funnel._slot_3.move_to_position(100)
        #    funnel._slot_3.wait_for_move()
        #    funnel._slot_3.move_to_position(0)
        #    funnel._slot_3.wait_for_move()




        #funnel.slot_2.home()
        
        #funnel._slot_2.move_relative(-10000)

        #funnel.slot_3.home()
        #funnel._slot_3.safety_off()
        ##funnel._slot_3.set_speed(100)
        #funnel.slot_3.move_to_position(100)
        #funnel.slot_3.wait_for_move()
        #funnel.slot_1.move_to_position(300)
        #funnel.slot_2.move_to_position(300)
        #funnel.slot_3.move_to_position(300)
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
