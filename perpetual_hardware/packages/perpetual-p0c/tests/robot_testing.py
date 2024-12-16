from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration, Funnel
from perpetual_p0c.tool import ToolConfiguration, Tool
from perpetual_p0c.tray import Tray
from perpetual_p0c.common import StorageModuleSide
from perpetual_p0c.configuration import p0_funnel_config, p0_workstation_configuration, p0_gantry_config, p0_tool_config
from perpetual_p0c.utils import get_can_devices
import time
import atexit
from perpetual_p0c.utils import get_bus
from typing import List


class RobotConfiguration(BaseModel):
    funnel_configuration: FunnelConfiguration
    gantry_configuration: GantryConfiguration
    tool_configuration: ToolConfiguration
    gantry_safe_x: float
    gantry_safe_y: float
    gantry_safe_y_high_clearance: float
    place_offset: GantryCoordinate
    service_coordinate_high: GantryCoordinate
    service_coordinate_low: GantryCoordinate
    idle_coordinate: GantryCoordinate
    output_location: GantryCoordinate
    input_location: GantryCoordinate


class P0C:
    #def __init__(self, bus: BusABC, config: RobotConfiguration):
    def __init__(self, config: RobotConfiguration):
        self._bus = get_bus()
        self._config = config
        self._tool = Tool(self._bus, config.tool_configuration)
        self._funnel = Funnel(self._bus, config.funnel_configuration)
        self._gantry = Gantry(self._bus, config.gantry_configuration)
        atexit.register(self.shutdown)

    def shutdown(self):
        self._tool.cleanup()
        self._gantry.cleanup()
        self._funnel.cleanup()
        self._bus.shutdown()


    def stop(self) -> None:
        self.tool.stop()
        self.funnel.stop()
        self.gantry.stop()

    @property
    def config(self) -> RobotConfiguration:
        return self._config

    @property
    def gantry(self) -> Gantry:
        return self._gantry

    @property
    def funnel(self) -> Funnel:
        return self._funnel

    @property
    def tool(self) -> Tool:
        return self._tool

    def home(self) -> None:
        self._tool.home()
        self._funnel.home()
        self._gantry.home()


    def path_plan(self, coordinate: GantryCoordinate) -> List[GantryCoordinate]:
        gantry_position = self.gantry.position
        path: List[GantryCoordinate] = []
        # Target is beyod safe x
        if gantry_position.x > self.config.gantry_safe_x:
            print(gantry_position.y, self.config.gantry_safe_y_high_clearance)
            # Above High Clearance
            if gantry_position.y >= self.config.gantry_safe_y_high_clearance - 1:
                if coordinate.y >= gantry_position.y-1:
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    if coordinate.x < self.config.gantry_safe_x:
                        path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=gantry_position.y))
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
                    else:
                        path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=gantry_position.y))
                        path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=self.config.gantry_safe_y))
                        path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
            # Below High Clearance
            else:
                if coordinate.y >= self.config.input_location.y+1:
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=coordinate.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
        else:
            if coordinate.x >= self.config.gantry_safe_x:
                if coordinate.y >= self.config.gantry_safe_y_high_clearance-1:
                    path.append(GantryCoordinate(x=gantry_position.x, y=coordinate.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
            else:
                path.append(coordinate)
                return path


    def gantry_move(self, coordinate: GantryCoordinate) -> None:
        if self._tool.check_tray_clear():
            path = self.path_plan(coordinate)
            for coordinate in path:
                self.gantry.move_coordinate(coordinate)
        else:
            raise Exception("Clear Break Beams")

    def pull_tray(self, coordinate: GantryCoordinate, side: StorageModuleSide) -> None:
        if self.tool.check_tray_loaded():
            raise Exception("The tool is blocked from pulling a tray")
        else:
            if side == StorageModuleSide.RIGHT:
                self.tool.safe_max()
            else:
                self.tool.safe_min()
            self.gantry_move(coordinate)
            self.tool.extend_puller(side)
            robot.tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
            time.sleep(.25)
            self.tool.retract_puller(side)
            if not self.tool.check_tray_loaded():
                raise Exception("Failed Tray Pull")
    
    def place_tray(self, coordinate: GantryCoordinate, side: StorageModuleSide) -> None:
        if self.tool.check_tray_loaded():
            self.gantry_move(coordinate + self._config.place_offset)
            self.tool.extend_puller(side)
            self.tool.set_solenoids_off()
            time.sleep(.25)
            self.tool.puller_safe(side)
            if not self.tool.check_tray_clear():
                raise Exception("Failed Tray Place")
        else:
            raise Exception("No Tray is Loaded")

    def move_gantry_to_input(self, tray: Tray) -> None:
        input = self.config.input_location
        input.y += tray.cell_depth
        self.gantry_move(input)

    def move_gantry_to_output(self) -> None:
        self.gantry_move(self.config.output_location)

    def service_high(self) -> None:
        self.gantry_move(self.config.service_coordinate_high)

    def service_low(self) -> None:
        self.gantry_move(self.config.service_coordinate_low)

    def idle(self) -> None:
        self.gantry_move(self.config.idle_coordinate)

    def move_gantry_to_clear(self, tray: Tray) -> None:
        self.gantry_move(
            GantryCoordinate(
            x=self._config.output_location.x,
            y=self._config.output_location.y + tray.cell_depth,
        ))

    def setup_receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.pull_tray(tray_location, side)
        self.move_gantry_to_input(tray)
        self._funnel.move_to_cell_catch(tray, index)

    def receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.setup_receive_item(tray, tray_location, index, side)
        self.pull_tray(tray_location, side)
        self.move_gantry_to_input(tray)

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

tray_1_coordinate = GantryCoordinate(x=175, y= 532)
tray_2_coordinate = GantryCoordinate(x=245, y= 532)
idle_coordinate = GantryCoordinate(x=100, y=100)


if __name__ == '__main__':
    can_bus = get_can_devices()
    robot_configuration = RobotConfiguration(
        funnel_configuration=p0_funnel_config,
        gantry_configuration=p0_gantry_config,
        tool_configuration=p0_tool_config,
        gantry_safe_x=320,
        gantry_safe_y=100,
        gantry_safe_y_high_clearance=1100,
        place_offset=GantryCoordinate(x=0, y=2),
        output_location=GantryCoordinate(x=676,y=1050),
        input_location=GantryCoordinate(x=676,y=288),
        service_coordinate_high=GantryCoordinate(x=100,y=1100),
        service_coordinate_low=GantryCoordinate(x=100,y=200),
        idle_coordinate=GantryCoordinate(x=100,y=100)
    )
    robot = P0C(robot_configuration)
    #robot.gantry.home()
    robot.home()

    """
    input() 
    pick_tray(robot, tray_1_coordinate)
    input()
    robot.gantry.move_coordinate(idle_coordinate)
    input()
    place_tray(robot, tray_1_coordinate)
    input()
    robot.gantry.move_coordinate(idle_coordinate)
    input()
    """
    #robot.gantry.move_coordinate(GantryCoordinate(x=100, y=100))
    #input()
    #robot.gantry.move_coordinate(GantryCoordinate(x=675, y=100))
    #robot.gantry.move_coordinate(GantryCoordinate(x=676, y=438))
    #robot.gantry.move_coordinate(GantryCoordinate(x=676, y=100))
    #robot.gantry.move_coordinate(GantryCoordinate(x=100, y=100))
    #robot.gantry.home()
    #robot.gantry.move_coordinate(GantryCoordinate(x=320, y=1050))
    #robot.tool.retract_puller(StorageModuleSide.RIGHT)
    #robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    robot.move_gantry_to_output()
    #robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.move_gantry_to_input(Tray(cells=6,cell_width=50, cell_depth=150))
    #robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
    #robot.gantry_move(GantryCoordinate(x=100, y=1000))
    #input()
    """
    for i in range(0, 5):
        robot.pull_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
        robot.gantry_move(idle_coordinate)
        robot.place_tray(tray_1_coordinate, StorageModuleSide.RIGHT)
        robot.gantry_move(idle_coordinate)

        robot.pull_tray(tray_2_coordinate, StorageModuleSide.RIGHT)
        robot.gantry_move(idle_coordinate)
        robot.place_tray(tray_2_coordinate, StorageModuleSide.RIGHT)
        robot.gantry_move(idle_coordinate)
    #robot.tool.puller.move_to_position(22)
    """

