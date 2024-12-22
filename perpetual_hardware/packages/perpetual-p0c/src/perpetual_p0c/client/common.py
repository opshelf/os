from abc import ABCMeta, abstractmethod
from enum import Enum
# from pydantic import BaseModel
from perpetual_p0c.gantry import GantryCoordinate

# from perpetual_p0c.funnel import FunnelConfiguration, Funnel
# from perpetual_p0c.tool import ToolConfiguration, Tool
# from perpetual_p0c.io_board import IoController
# from perpetual_p0c.utils import get_bus
# from typing import List, Optional
# from perpetual_p0c.workstation import Workstation, WorkstationConfiguration
from perpetual_p0c.size_database import SizeDatabase
from perpetual_p0c.storage import CellItem, GS1Data, StorageGrid, StorageModuleCellLocation, Tray

"""
class RobotClient(ABCMeta):

    def save_state(self) -> None:
        ...

    def save_sizes(self) -> None:
        ...

    def load_sizes(self) -> None:
        ...

    def load_state(self) -> None:
        ...

    def load_storage(self) -> None:
        ...

    def save_storage(self) -> None:
        ...

    def shutdown(self) -> None:
        ...
    
    def stop(self) -> None:
        ...

    @property
    def config(self) -> RobotConfiguration:

    @property
    def gantry(self) -> Gantry:

    @property
    def funnel(self) -> Funnel:

    @property
    def tool(self) -> Tool:

    @property
    def workstation(self) -> Workstation:

    @property
    def size_db(self) -> SizeDatabase:

    @property
    def storage(self) -> StorageGrid:

    def home(self) -> None:

    def path_plan(self, coordinate: GantryCoordinate) -> List[GantryCoordinate]:

    def gantry_move(self, coordinate: GantryCoordinate) -> None:

    def pull_tray(self, coordinate: GantryCoordinate, tray: Tray, side: StorageModuleSide) -> None:

    def place_tray(self, coordinate: GantryCoordinate, side: StorageModuleSide) -> None:

    def move_gantry_to_input(self, tray: Tray) -> None:

    def move_gantry_to_output(self) -> None:

    def service_high(self) -> None:

    def service_low(self) -> None:

    def idle(self) -> None:

    def move_gantry_to_clear(self, tray: Tray) -> None:

    def run_output_transfer(self) -> None:

    def move_gantry_to_input_location(self) -> None:

    def run_input_transfer(self, tray: Tray, idx: int) -> None:

    def run_storage_operation(self, operation: Operation) -> None:

    def move_to_storage_location(self, location: StorageModuleCellLocation) -> None:

    def _place_tray(self, location: StorageModuleCellLocation | StorageModuleTrayLocation) -> None:

    def return_current_tray(self) -> None:

    def _pull_tray(self, location: StorageModuleTrayLocation) -> None:

    def _setup_receive_item(self, location: StorageModuleCellLocation) -> None:

    def _receive_item(self, location: StorageModuleCellLocation) -> None:

    def _drop_off_item(self, location: StorageModuleCellLocation) -> None:

    def store_item(self, item: CellItem) -> None:

    def store_item_gs1_data(self, data: GS1Data) -> None:

    def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:

    def retrieve_item_by_gtin(self, data: GS1Data) -> None:
"""


class RobotClient(ABCMeta):
    def save_state(self) -> None: ...

    def save_sizes(self) -> None: ...

    def load_sizes(self) -> None: ...

    def load_state(self) -> None: ...

    def load_storage(self) -> None: ...

    def save_storage(self) -> None: ...

    def shutdown(self) -> None: ...

    def stop(self) -> None: ...

    # @property
    # def size_db(self) -> SizeDatabase:
    #    ...

    # @property
    # def storage(self) -> StorageGrid:
    #    ...

    def home(self) -> None: ...

    def home_y_first(self) -> None: ...

    def gantry_move(self, coordinate: GantryCoordinate) -> None: ...

    def move_gantry_to_input(self, tray: Tray) -> None: ...

    def move_gantry_to_output(self) -> None: ...

    def service_high(self) -> None: ...

    def service_low(self) -> None: ...

    def idle(self) -> None: ...

    def move_gantry_to_clear(self, tray: Tray) -> None: ...

    def run_output_transfer(self) -> None: ...

    def move_gantry_to_input_location(self) -> None: ...

    def run_input_transfer(self, tray: Tray, idx: int) -> None: ...

    def move_to_storage_location(self, location: StorageModuleCellLocation) -> None: ...

    def return_current_tray(self) -> None: ...

    def store_item(self, item: CellItem) -> None: ...

    def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None: ...

    def retrieve_item_by_gtin(self, data: GS1Data) -> None: ...


"""
What Andrey Cares About

class RobotClient(ABCMeta):

    @property
    def size_db(self) -> SizeDatabase:
        ...

    @property
    def storage(self) -> StorageGrid:
        ...

    def home(self) -> None:
        ...

    def home_y_first(self) -> None:
        ...

    def gantry_move(self, coordinate: GantryCoordinate) -> None:
        ...

    def move_gantry_to_input(self, tray: Tray) -> None:
        ...

    def move_gantry_to_output(self) -> None:
        ...

    def service_high(self) -> None:
        ...

    def service_low(self) -> None:
        ...

    def idle(self) -> None:
        ...

    def move_gantry_to_clear(self, tray: Tray) -> None:
        ...

    def run_output_transfer(self) -> None:
        ...

    def move_gantry_to_input_location(self) -> None:
        ...

    def run_input_transfer(self, tray: Tray, idx: int) -> None:
        ...

    def move_to_storage_location(self, location: StorageModuleCellLocation) -> None:
        ...

    def return_current_tray(self) -> None:
        ...

    def store_item(self, item: CellItem) -> None:
        ...

    def store_item_gs1_data(self, data: GS1Data) -> None:
        ...

    def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:
        ...

    def retrieve_item_by_gtin(self, data: GS1Data) -> None:
        ...

"""



class RobotStatus(int, Enum):
    ERROR = 0x00
    IDLE = 0x01
    BUSY = 0x02
    LOW_LEVEL = 0x03


