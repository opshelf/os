from perpetual_p0c.gantry import Gantry
from perpetual_p0c.funnel import Funnel
from perpetual_p0c.tool import Tool
from perpetual_p0c.configuration import p0_gantry_config, p0_tool_config, p0_funnel_config
from perpetual_p0c.tool_board import IoBoard
from perpetual_p0c.storage import Tray, StorageModuleSide
from perpetual_p0c.utils import get_bus
import time

CLEARANCE = 450

def test_pick() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        tool = Tool(bus, p0_tool_config)
        #gantry.home()
        tool.puller.move_to_position(470)
        tool.puller.wait_for_move()
        gantry.move(x=175, y=528)
        #tool.home()
        tool.puller.move_to_position(480)
        tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
        time.sleep(.25)
        tool.puller.move_to_position(480-350)
        tool.puller.wait_for_move()

def test_pickup_location() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        #gantry.home()
        tool = Tool(bus, p0_tool_config)
        funnel = Funnel(bus, p0_funnel_config)
        #funnel.home()
        #tool.set_solenoids_off()
        #tool.puller.

        #tool.home()
        tool.puller.move_to_position(36)
        tool.puller.wait_for_move()
    
        #gantry.move(x=680, y=100)
        gantry.move(x=675, y=438)

def test_dropoff_location() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        #gantry.home()
        tool = Tool(bus, p0_tool_config)
        funnel = Funnel(bus, p0_funnel_config)
        #gantry.move(x=675, y=100)
        #gantry.move(x=175, y=100)
        #gantry.move(x=175, y=528)
        gantry.y_axis.home_with_brake()    
        gantry.home()
        gantry.move(x=175, y=534)
        #tool.puller.move_to_position(480)
        #funnel.home()
        #tool.set_solenoids_off()
        #tool.puller.

        #tool.home()
        #tool.puller.move_to_position(36)
        #tool.puller.wait_for_move()
    
        #gantry.move(x=680, y=100)

def test_repeat_pick_place() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        tool = Tool(bus, p0_tool_config)
        tool.home()
        tool.set_solenoids_off()
        tool.actuators(False)
        input()
        gantry.home()
        
        for i in range(0, 10):
            input()
            gantry.move(x=175, y=528)
            tool.puller.move_to_position(480)
            tool.puller.wait_for_move()
            tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
            time.sleep(.25)
            tool.puller.move_to_position(36)
            tool.puller.wait_for_move()
            gantry.move(x=100, y=100)
            input()
            gantry.move(x=175, y=534)
            tool.puller.move_to_position(480)
            tool.puller.wait_for_move()
            tool.set_solenoids_off()
            time.sleep(.25)
            tool.puller.move_to_position(400)
            tool.puller.wait_for_move()
            input()
            gantry.move(x=100, y=100)
            input()
        #tool.puller.move_to_position(480)
        #funnel.home()
        #tool.set_solenoids_off()
        #tool.puller.

        #tool.home()
        #tool.puller.move_to_position(36)
        #tool.puller.wait_for_move()
    
        #gantry.move(x=680, y=100)

def service() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        tool = Tool(bus, p0_tool_config)
        #tool.home()
        #gantry.home()
        tool.puller.move_to_position(200)
        gantry.move(x=175, y=1200)
        #tool.actuators(True)

# Initial Position 250mm
# Width 205mm
# Min Transfer Position: 315

def catch() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        gantry.home()
        #gantry.move(x=100, y=1100)
        #gantry.move(x=600, y=1100)
        funnel = Funnel(bus, p0_funnel_config)
        #funnel.home()
        #250
        #funnel.opposing_funnel.move_to_position(250)
        #funnel.opposing_funnel.home()
        #input()
        #funnel.opposing_funnel.move_to_position(funnel._configuration.opposing_funnel_max_travel)
        funnel.opposing_funnel.move_to_position(315)

        #tool = Tool(bus, p0_tool_config)

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




def catch_slots() -> None:
    can_bus = get_bus()
    with can_bus as bus:
        tool = Tool(bus, p0_tool_config)
        tool.actuators(False)
        gantry = Gantry(
            bus,
            p0_gantry_config,
        )
        #gantry.home()
        #gantry.move(x=100, y=1100)
        gantry.move(x=600, y=1100)
        funnel = Funnel(bus, p0_funnel_config)
        #funnel.inlet_funnel.home()
        #funnel.opposing_funnel.home()
        tray = Tray(width=50, height=150)
        for i in range(0, tray.cells):
            position = calculate_slot_position(Tray(width=50, height=150), i)
            print(position)
            funnel.opposing_funnel.move_to_position(position)
            funnel.opposing_funnel.wait_for_move()
            tool.set_slot_bits(1 << (tray.cells - i - 1))
            tool.actuators(True)
            time.sleep(1)
            input()
            tool.actuators(False)
            tool.set_slot_bits(0)
            input()





if __name__ == "__main__":
    #test_pick()
    #test_pickup_location()
    #test_dropoff_location()
    #test_repeat_pick_place()
    #catch()
    catch_slots()
    #service()

    #calculate_slot_position(Tray(cells=6, cell_width=50, cell_depth=150))
    """
    class Tray(BaseModel):
        cells: int
        cell_width: float
        cell_depth: float
        wall_size: float = 5.0
        tray_size: float = 335.0
    
        def get_cell_position(self, idx: int) -> Tuple[float, float]:
            cell_width = (self.tray_size - (2 + self.cells - 1) * self.wall_size) / self.cells
            start_position = self.wall_size + cell_width * idx + self.wall_size * idx
            return (start_position, start_position + cell_width)
    """
