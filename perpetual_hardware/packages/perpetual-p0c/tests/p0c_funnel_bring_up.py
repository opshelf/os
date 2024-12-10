from perpetual_p0c.funnel import Funnel
from perpetual_p0c.configuration import p0_funnel_config
from can import Bus
from perpetual_p0c.utils import get_can_devices
import time
from perpetual_p0c.tray import Tray

#class Tray(BaseModel):
#    cells: int
#    cell_width: float
#    cell_depth: float
#    wall_size: float = 5.0
#    tray_size: float = 335.0
#
#    def get_cell_position(self, idx: int) -> Tuple[float, float]:
#        cell_width = (self.tray_size - (2 + self.cells - 1) * self.wall_size) / self.cells
#        start_position = self.wall_size + cell_width * idx + self.wall_size * idx
#        return (start_position, start_position + cell_width)


if __name__ == "__main__":
    usb_can = get_can_devices()
    with Bus(
        interface=usb_can.CAN_INTERFACE,
        channel=usb_can.CAN_CHANNEL,
        bitrate=usb_can.CAN_BITRATE,
        fd=False,
    ) as bus:
        print(bus)
        funnel = Funnel(bus, p0_funnel_config)
        funnel.stop()
        funnel.home()
        #for i in range(0, 200, 10):
        #    funnel.inlet_funnel.move_to_position(i)
        #    funnel.inlet_funnel.wait_for_move()
        #    input()

        #funnel._opposing_funnel.home()
        #funnel._opposing_funnel.move_to_position(217)
        #funnel._opposing_funnel.wait_for_move()

        #funnel._inlet_funnel.move_to_position(0)
        #funnel._inlet_funnel.wait_for_move()
        #funnel.stop()
        time.sleep(10)
        for slot in range(0, 4):
            funnel.move_to_cell(Tray(cells=6,cell_width=50, cell_depth=100), slot)
            time.sleep(10)
        
        #funnel.slot_1.home()
        #funnel.slot_1.move_to_position(0)
        #funnel.slot_1.wait_for_move()
        #funnel.slot_1.move_to_position(100)
        #funnel.slot_1.wait_for_move()
        #funnel.slot_1.home()
        #funnel.slot_2.home()
        #funnel.slot_3.home()

        #input()
        #funnel.slot_1.move_to_position(0)
        #funnel.slot_2.move_to_position(0)
        #funnel.slot_3.move_to_position(0)
        #funnel.slot_1.wait_for_move()
        #funnel.slot_2.wait_for_move()
        #funnel.slot_3.wait_for_move()
        #funnel.slot_1.move_to_position(100)
        #funnel.slot_2.move_to_position(100)
        #funnel.slot_3.move_to_position(100)
        #funnel.slot_1.wait_for_move()
        #funnel.slot_2.wait_for_move()
        #funnel.slot_3.wait_for_move()
        
        #input()
        #funnel.slot_1.home()
        #input()
        #funnel.slot_2.home()
        #input()
        #funnel.slot_2.home()
        #input()
        #funnel.slot_3.home()

        #for i in range(0, 10):
        #    #funnel.slot_1.move_to_position(0)
        #    #funnel.slot_1.wait_for_move()
        #    #funnel.slot_1.move_to_position(100)
        #    #funnel.slot_1.wait_for_move()

        #    funnel.slot_3.move_to_position(0)
        #    funnel.slot_3.wait_for_move()
        #    funnel.slot_3.move_to_position(100)
        #    funnel.slot_3.wait_for_move()

        #for i in range(0, 10):
        #    funnel.slot_1.move_to_position(0)
        #    funnel.slot_2.move_to_position(0)
        #    funnel.slot_3.move_to_position(0)
        #    funnel.slot_1.wait_for_move()
        #    funnel.slot_2.wait_for_move()
        #    funnel.slot_3.wait_for_move()
        #    funnel.slot_1.move_to_position(100)
        #    funnel.slot_2.move_to_position(100)
        #    funnel.slot_3.move_to_position(100)
        #    funnel.slot_1.wait_for_move()
        #    funnel.slot_2.wait_for_move()
        #    funnel.slot_3.wait_for_move()

        #funnel.home()
        #funnel.inlet_funnel.move_to_position(6)
        #funnel.inlet_funnel.wait_for_move()
        print("Stopping")
        #funnel.opposing_funnel.stop_motor()
        #funnel.home()
        #input()
        #funnel.start_belt()
        #input()
        #funnel.stop_belt()
        #input()
        #funnel.start_wall()
        #input()
        #funnel.stop_wall()
        #input()
        #funnel.start_output()
        #input()
        #funnel.stop_output()
        #input()
        #funnel.start_output_transfer()
        #input()
        #funnel.stop_output_transfer()
