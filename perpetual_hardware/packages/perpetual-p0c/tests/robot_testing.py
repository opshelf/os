from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration, Funnel, FunnelOpeningSize
from perpetual_p0c.tool import ToolConfiguration, Tool
from perpetual_p0c.tray import Tray
from perpetual_p0c.common import StorageModuleSide
from perpetual_p0c.configuration import p0_robot_config
from perpetual_p0c.utils import get_can_devices
from perpetual_p0c.robot import P0C
import time



"""
def init(self) -> None:
    self._tool.set_slot_bits(0)
    self._tool.set_carriage_solenoids(0, StorageModuleSide.LEFT)
    self._tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
    self._tool.actuators(False)
    self._funnel.open_all_slots()

def deposit_item(
    self,
    tray: Tray,
    tray_location: GantryCoordinate,
    index: int,
    side: StorageModuleSide,
) -> None:
    self.pull_tray(tray_location, side)
    self._funnel.catch()
    self.move_gantry_to_output()
    self.drop_item(tray, index)
    self.place_tray(tray_location, side)

def drop_item(self, tray: Tray, index: int) -> None:
    self._tool.set_slot_bits(0)
    self._funnel.catch()
    self._tool.set_solenoids(tray, index)
    self._tool.actuators(True)
    time.sleep(2)
    self.move_gantry_to_clear(tray)
    self._tool.actuators(False)
    time.sleep(2)
    self._tool.set_slot_bits(0)


"""

#tray_1_coordinate = GantryCoordinate(x=175, y= 528)
#tray_2_coordinate = GantryCoordinate(x=245, y= 528)

tray_1_coordinate = GantryCoordinate(x=175, y= 522)
tray_2_coordinate = GantryCoordinate(x=245, y= 522)
tray_3_coordinate = GantryCoordinate(x=315, y= 522)

idle_coordinate = GantryCoordinate(x=100, y=100)

def calculate_slot_position(tray: Tray, idx: int) -> float: 
    max_travel = 375 
    min_travel = 250
    conveyor_width = 205
    #min_transfer_location = 220 
    start, _ = tray.get_cell_position(idx)
    position = max_travel - start + conveyor_width/2
    position = max(position, min_travel)
    position = min(position, max_travel)
    return position


if __name__ == '__main__':
    can_bus = get_can_devices()
    robot = P0C(p0_robot_config)
    #robot.gantry.home()
    #robot.home()
    #robot.tool.puller.move_to_position(481.5)
    #robot.tool.puller.wait_for_move()

    #robot.move_gantry_to_clear(Tray(cells=6,cell_width=50, cell_depth=150))
    #robot.gantry.y_axis.move_to_relative_position_with_brake(100)
    #robot.gantry.home()
    #input()
    #robot.move_gantry_to_output()
    #robot.gantry.y_axis.move_to_position_with_brake(385)
    #robot.gantry.y_axis.wait_for_brake_move()
    #robot.gantry.y_axis.home_with_brake()



    #for i in range(0, 10):
    #    robot.idle()
    #    robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #    robot.move_gantry_to_input(Tray(cells=6,cell_width=50, cell_depth=200))
    #    robot.move_gantry_to_output()
    #    robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)

    #    robot.idle()
    #    robot.pull_tray(tray_2_coordinate, StorageModuleSide.RIGHT)
    #    robot.move_gantry_to_input(Tray(cells=6,cell_width=50, cell_depth=100))
    #    robot.move_gantry_to_output()
    #    robot.place_tray(tray_2_coordinate, StorageModuleSide.RIGHT)

    #robot.idle()
    #robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.move_gantry_to_input(Tray(cells=6,cell_width=50, cell_depth=200))
    #robot.idle()
    #robot.tool.puller.home()
    #robot.idle()
    ##robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)

    #robot.pull_tray(tray_2_coordinate, StorageModuleSide.RIGHT)
    #robot.place_tray(tray_2_coordinate, StorageModuleSide.RIGHT)
    robot.tool.home()
    for i in range(0, 10):
        robot.pull_tray(tray_3_coordinate, StorageModuleSide.RIGHT)
        robot.place_tray(tray_3_coordinate, StorageModuleSide.RIGHT)
    #robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.home()
    #robot.gantry.move_coordinate(tray_3_coordinate)
    #robot.idle()
    #robot.funnel.home()
    #input()
    #robot.funnel._slot_1.safety_off()
    #robot.funnel._slot_2.safety_off()
    #robot.funnel._slot_3.safety_off()
    #robot.funnel._slot_1.move_to_position(300)
    #robot.funnel._slot_2.move_to_position(300)
    #robot.funnel._slot_3.move_to_position(300)
    #for i in range(0, 10):
    #    robot.funnel.slot_1.move_to_position(100)
    #    robot.funnel.slot_2.move_to_position(100)
    #    robot.funnel.slot_3.move_to_position(100)
    #    
    #    robot.funnel.slot_1.wait_for_move()
    #    robot.funnel.slot_2.wait_for_move()
    #    robot.funnel.slot_3.wait_for_move()

    #    robot.funnel.slot_1.move_to_position(0)
    #    robot.funnel.slot_2.move_to_position(0)
    #    robot.funnel.slot_3.move_to_position(0)

    #    robot.funnel.slot_1.wait_for_move()
    #    robot.funnel.slot_2.wait_for_move()
    #    robot.funnel.slot_3.wait_for_move()
    
    #input()
    #for i in range(0, 3):
    #    robot.receive_item(
    #        Tray(cells=6,cell_width=50, cell_depth=200),
    #        tray_1_coordinate,
    #        i,
    #        StorageModuleSide.RIGHT
    #    )
    #robot.idle()
    #for i in range(0, 6):
    #    robot.drop_off_item(
    #        Tray(cells=6,cell_width=50, cell_depth=200),
    #        tray_3_coordinate,
    #        i,
    #        StorageModuleSide.RIGHT
    #    )
    
    #robot.run_input_transfer()
    
    #robot.move_gantry_to_output()
    #robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)

