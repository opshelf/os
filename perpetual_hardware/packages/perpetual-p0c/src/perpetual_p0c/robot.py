from can import BusABC
from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration, Funnel
from perpetual_p0c.tool import ToolConfiguration, Tool
from perpetual_p0c.tray import Tray
from perpetual_p0c.common import StorageModuleSide
import time


class RobotConfiguration(BaseModel):
    funnel_configuration: FunnelConfiguration
    gantry_configuration: GantryConfiguration
    tool_configuration: ToolConfiguration
    gantry_safe_x: float
    place_offset: GantryCoordinate
    output_location: GantryCoordinate
    input_location: GantryCoordinate


class P0Robot:
    def __init__(self, bus: BusABC, config: RobotConfiguration):
        self._config = config
        self._tool = Tool(bus, config.tool_configuration)
        self._funnel = Funnel(bus, config.funnel_configuration)
        self._gantry = Gantry(bus, config.gantry_configuration)

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
        self.tool.home()
        self.funnel.home()
        self.gantry.home()

    def stop(self) -> None:
        self.tool.stop()
        self.funnel.stop()
        self.gantry.stop()

    def init(self) -> None:
        self._tool.set_slot_bits(0)
        self._tool.set_carriage_solenoids(0, StorageModuleSide.LEFT)
        self._tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
        self._tool.actuators(False)
        self._funnel.open_all_slots()

    def move_gantry_safe(self, location: GantryCoordinate) -> None:
        gantry_position = self._gantry.position
        if gantry_position.x > self._config.gantry_safe_x:
            self._gantry.move(self._config.gantry_safe_x, gantry_position.y)
            self._gantry.move(location.x, location.y)
        else:
            self._gantry.move(location.x, location.y)

    def pull_tray(self, tray_location: GantryCoordinate, side: StorageModuleSide) -> None:
        self.move_gantry_safe(tray_location)
        self._tool.extend_puller()
        self._tool.set_carriage_solenoids(1, side)
        self._tool.retract_tray(side)

    def place_tray(self, tray_location: GantryCoordinate, side: StorageModuleSide) -> None:
        self.move_gantry_safe(tray_location + self._config.place_offset)
        self._tool.extend_puller()
        self._tool.wait_for_move()
        self._tool.set_carriage_solenoids(0, side)
        self._tool.retract_tray(side)
        self._tool.wait_for_move()

    def receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.pull_tray(tray_location, side)
        input()
        self.move_gantry_to_input(tray)
        self._funnel.move_to_cell_catch(tray, index)

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

    def move_gantry_to_input(self, tray: Tray) -> None:
        gantry_position = self._gantry.position
        if gantry_position.x > self._config.gantry_safe_x:
            self._gantry.move(self._config.gantry_safe_x, gantry_position.y)
        else:
            self._gantry.move(gantry_position.x, 0)
        x = self._config.input_location.x + tray.cell_width + 2 * tray.wall_size
        y = self._config.input_location.y + tray.cell_depth
        self._gantry.move(x, 0)
        self._gantry.move(x, y)

    def move_gantry_to_output(self) -> None:
        gantry_position = self._gantry.position
        if gantry_position.x > self._config.gantry_safe_x:
            self._gantry.move(self._config.gantry_safe_x, gantry_position.y)
        else:
            self._gantry.move(gantry_position.x, self._config.output_location.y)
        self._gantry.move(self._config.output_location.x, self._config.output_location.y)

    def move_gantry_to_clear(self, tray: Tray) -> None:
        self._gantry.move(
            self._config.output_location.x,
            self._config.output_location.y + tray.cell_depth,
        )


# class Commands(BaseModel):
#    GANTRY = "GANTRY"
#    FUNNEL = "FUNNEL"
#    TOOL = "TOOL"
#    RECEIVE_ITEM
#    DEPOSIT_ITEM
