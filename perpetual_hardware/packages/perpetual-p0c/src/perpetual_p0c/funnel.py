from hardware_common.stepper_motor import AbstractStepperMotor, AxisCommand
from can import BusABC
from .tray import Tray
from .common import CanAxisConfiguration
from perpetual_trinamic.trinamic_axis import TrinamicAxis, TrinamicStepper
from pydantic import BaseModel
from typing import Optional, Dict, Any
from enum import Enum
from .io_controller import (
    ToolRelayController,
    GenericRelayBoardMessage,
)
import time


class FunnelOffsets(BaseModel):
    funnel_min_width: float
    funnel_min_height: float
    funnel_cell_1_offset_x: float
    funnel_cell_1_offset_y: float
    catch_position: float
    diverter_50mm_pos: float
    diverter_100mm_pos: float
    diverter_150mm_pos: float
    belt_speed: float = 1500
    wall_speed: float = 1500
    output_transfer_conveyor_speed: float = 1500
    output_conveyor_speed: float = 1500


class FunnelConfiguration(BaseModel):
    inlet_funnel: CanAxisConfiguration
    opposing_funnel: CanAxisConfiguration
    slot_1: CanAxisConfiguration
    slot_2: CanAxisConfiguration
    slot_3: CanAxisConfiguration
    belt: CanAxisConfiguration
    wall: CanAxisConfiguration
    output_transfer_conveyor: CanAxisConfiguration
    output_conveyor: CanAxisConfiguration
    diverter: CanAxisConfiguration
    relay_board_id: int
    offsets: FunnelOffsets
    opposing_funnel_max_travel: float = 270.0


class FunnelStatus(BaseModel):
    inlet_funnel: float
    opposing_funnel: float
    slot_1: float
    slot_2: float
    slot_3: float


class FunnelOpeningSize(int, Enum):
    OPEN_50MM = 0
    OPEN_100MM = 1
    OPEN_150MM = 2


class Commands(str, Enum):
    HOME = "HOME"
    STOP = "STOP"
    MOVE_TO_CELL = "MOVE_TO_CELL"
    CLOSE_FUNNEL = "CLOSE_FUNNEL"
    INLET_COMMAND = "INLET_COMMAND"
    OPPOSING_FUNNEL_COMMAND = "OPPOSING_FUNNEL_COMMAND"
    START_BELT = "START_BELT"
    STOP_BELT = "STOP_BELT"
    WIGGLE = "WIGGLE"
    INITIALIZE = "INITIALZE"


class MoveToCellCommand(BaseModel):
    tray: Tray
    idx: int


class FunnelCommands(BaseModel):
    command: Commands
    payload: Optional[Dict[Any, Any]]


class WiggleCommand(BaseModel):
    wiggle: float


class Funnel:
    def __init__(self, bus: BusABC, funnel_configuration: FunnelConfiguration):
        self._configuration = funnel_configuration
        self._inlet_funnel = TrinamicAxis(
            bus,
            funnel_configuration.inlet_funnel.can_id,
            0,
            funnel_configuration.inlet_funnel.configuration,
        )
        self._opposing_funnel = TrinamicAxis(
            bus,
            funnel_configuration.opposing_funnel.can_id,
            0,
            funnel_configuration.opposing_funnel.configuration,
        )
        self._slot_1 = TrinamicAxis(
            bus,
            funnel_configuration.slot_1.can_id,
            0,
            funnel_configuration.slot_1.configuration,
        )
        self._slot_2 = TrinamicAxis(
            bus,
            funnel_configuration.slot_2.can_id,
            0,
            funnel_configuration.slot_2.configuration,
        )
        self._slot_3 = TrinamicAxis(
            bus,
            funnel_configuration.slot_3.can_id,
            0,
            funnel_configuration.slot_3.configuration,
        )
        self._slots = ToolRelayController(bus, can_id=funnel_configuration.relay_board_id)
        self._belt = TrinamicStepper(
            bus,
            module_id=funnel_configuration.belt.can_id,
            configuration=funnel_configuration.belt.configuration,
            motor_id=0,
            max_current=6000,
        )
        self._wall = TrinamicStepper(
            bus,
            module_id=funnel_configuration.wall.can_id,
            configuration=funnel_configuration.wall.configuration,
            motor_id=0,
        )
        self._diverter = TrinamicStepper(
            bus,
            module_id=funnel_configuration.diverter.can_id,
            configuration=funnel_configuration.diverter.configuration,
            motor_id=0,
        )
        # self._output_transfer_conveyor = TrinamicStepper(
        #    bus,
        #    module_id=funnel_configuration.output_transfer_conveyor.can_id,
        #    configuration=funnel_configuration.output_transfer_conveyor.configuration,
        #    motor_id=0,
        # )
        self._slots_open = 0
        self._diverter_open = True

    @property
    def belt(self) -> TrinamicStepper:
        return self._belt

    @property
    def wall(self) -> TrinamicStepper:
        return self._wall

    # @property
    # def output_transfer_conveyor(self) -> TrinamicStepper:
    #    return self._output_transfer_conveyor

    # def output_transfer_conveyor_rotate(self, speed: int = -600) -> None:
    #    self._output_transfer_conveyor.rotate(speed)

    # def output_transfer_conveyor_stop(self) -> None:
    #    self._output_transfer_conveyor.stop()

    def stop_belt(self) -> None:
        self._belt.stop()
        self._wall.stop()

    def start_belt(self) -> None:
        # self._belt.rotate(1000)
        self._belt.initialize()
        self._wall.initialize()
        # self._belt.rotate(-2000)
        self._belt.rotate(-1500)
        self._wall.rotate(-2000)

    @property
    def offsets(self) -> FunnelOffsets:
        return self._configuration.offsets

    @offsets.setter
    def offsets(self, offsets: FunnelOffsets) -> None:
        self._configuration.offsets = offsets

    @property
    def slots(self) -> ToolRelayController:
        return self._slots

    @property
    def slots_open(self) -> int:
        return self._slots_open

    def open_slots(self, slots: int) -> None:
        self._slots_open = slots
        self._slots.set_relays(
            GenericRelayBoardMessage(
                r2=False if slots > 2 else True,
                r3=False if slots > 1 else True,
                r4=False if slots > 0 else True,
            )
        )

    def close_slots(self) -> None:
        self._slots_open = 0
        self._slots.set_relays(GenericRelayBoardMessage(r2=True, r3=True, r4=True))

    def open_all_slots(self) -> None:
        self._slots.set_relays(GenericRelayBoardMessage())


    @property
    def inlet_funnel(self) -> AbstractStepperMotor:
        return self._inlet_funnel

    @property
    def opposing_funnel(self) -> AbstractStepperMotor:
        return self._opposing_funnel

    @property
    def slot_1(self) -> AbstractStepperMotor:
        return self._slot_1

    @property
    def slot_2(self) -> AbstractStepperMotor:
        return self._slot_2

    @property
    def slot_3(self) -> AbstractStepperMotor:
        return self._slot_3

    @property
    def diverter(self) -> TrinamicStepper:
        return self._diverter

    def return_slots(self) -> None:
        self._slot_1.move_to_position(0)
        self._slot_2.move_to_position(0)
        self._slot_3.move_to_position(0)
        self.wait_for_move()

    def home(self) -> None:
        self._inlet_funnel.home()
        self._opposing_funnel.home()
        self._slot_1.home()
        self._slot_2.home()
        self._slot_3.home()
        self._diverter.home()

    def wait_for_move(self) -> None:
        self._inlet_funnel.wait_for_move()
        self._opposing_funnel.wait_for_move()
        self._slot_1.wait_for_move()
        self._slot_2.wait_for_move()
        self._slot_3.wait_for_move()

    def move_to_cell(self, tray: Tray, idx: int) -> None:
        self.inlet_funnel.move_to_position(0)
        self.opposing_funnel.move_to_position(0)
        self.return_slots()
        self.wait_for_move()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        # self._inlet_funnel_width.move_to_position(position)
        # self._opposing_funnel_width.move_to_position(position)

        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        if tray.cell_width <= 50:
            self.open_slots(1)
            # self._slot_1.move_to_position(0)
            # self._slot_2.move_to_position(diff-5)
            # self._slot_3.move_to_position(diff-5)
            self._slot_1.move_to_position(diff - 5)
            self._slot_2.move_to_position(diff - 5)
            self._slot_3.move_to_position(0)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            self.open_slots(2)
            # self._slot_1.move_to_position(0)
            # self._slot_2.move_to_position(0)
            # self._slot_3.move_to_position(diff-5)
            self._slot_1.move_to_position(diff - 5)
            self._slot_2.move_to_position(0)
            self._slot_3.move_to_position(0)
        else:
            self.open_slots(3)
            self._slot_1.move_to_position(0)
            self._slot_2.move_to_position(0)
            self._slot_3.move_to_position(0)

        if self._diverter_open:
            if tray.cell_width <= 50:
                self.set_diverter_opening(FunnelOpeningSize.OPEN_50MM)
            elif tray.cell_width > 50 and tray.cell_width <= 105:
                self.set_diverter_opening(FunnelOpeningSize.OPEN_100MM)
            else:
                self.set_diverter_opening(FunnelOpeningSize.OPEN_150MM)

        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos += self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.opposing_funnel_max_travel, ipos])
        # curr_ipos = self._inlet_funnel.get_position()
        opos = max([0, opos])
        curr_opos = self._opposing_funnel.get_position()
        # self._slot_2.move_to_position(opos-ipos)
        # self._slot_2.move_to_position(diff-5)
        # self._slot_3.move_to_position(opos-ipos)

        if opos >= curr_opos:
            self._opposing_funnel.move_to_position(opos)
            self._inlet_funnel.move_to_position(ipos)
            self.wait_for_move()
        else:
            self._inlet_funnel.move_to_position(ipos)
            self._opposing_funnel.move_to_position(opos)
            self.wait_for_move()

    def set_diverter_opening(self, size: FunnelOpeningSize) -> None:
        if size == FunnelOpeningSize.OPEN_50MM:
            self._diverter.move_revolutions(self._configuration.offsets.diverter_50mm_pos)
        elif size == FunnelOpeningSize.OPEN_100MM:
            self._diverter.move_revolutions(self._configuration.offsets.diverter_100mm_pos)
        elif size == FunnelOpeningSize.OPEN_150MM:
            self._diverter.move_revolutions(self._configuration.offsets.diverter_150mm_pos)
        else:
            raise Exception("Invalid Opening")
        self.wait_for_move()

    def return_diverter(self) -> None:
        self._diverter.move_revolutions(0.0)
        self.wait_for_move()

    def wiggle(self, revolutions: float = 0.2) -> None:
        curr_pos = self._diverter.get_position()
        try:
            self._diverter.move_revolutions(0)
            self._diverter.wait_for_move()
            time.sleep(0.25)
            self._diverter.move_revolutions(curr_pos)
            self._diverter.wait_for_move()
            self._diverter.move_relative_revolutions(revolutions)
            self._diverter.wait_for_move()
            self._diverter.move_relative_revolutions(-revolutions)
            self._diverter.wait_for_move()
            self._diverter.move_relative_revolutions(revolutions)
            self._diverter.wait_for_move()
            self._diverter.move_relative_revolutions(-revolutions)
            self._diverter.wait_for_move()
        except Exception as e:
            print(e)
            self._diverter.initialize()
            self._diverter.move_revolutions(curr_pos)
            self._diverter.wait_for_move()

    def move_to_cell_catch(self, tray: Tray, idx: int) -> None:
        self.inlet_funnel.move_to_position(0)
        self.opposing_funnel.move_to_position(0)
        self.return_slots()
        self.wait_for_move()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        slot_pos = diff - 5
        if tray.cell_width <= 50:
            self.open_slots(1)
            self._slot_1.move_to_position(slot_pos)
            self._slot_2.move_to_position(slot_pos)
            self._slot_3.move_to_position(slot_pos)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            self.open_slots(2)
            self._slot_1.move_to_position(slot_pos)
            self._slot_2.move_to_position(slot_pos)
            self._slot_3.move_to_position(slot_pos)
        else:
            self.open_slots(3)
            self._slot_1.move_to_position(slot_pos)
            self._slot_2.move_to_position(slot_pos)
            self._slot_3.move_to_position(slot_pos)
        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos += self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.opposing_funnel_max_travel, ipos])
        opos = max([0, opos])
        curr_opos = self._opposing_funnel.get_position()
        if opos >= curr_opos:
            self._opposing_funnel.move_to_position(opos)
            self._inlet_funnel.move_to_position(ipos)
            self.wait_for_move()
        else:
            self._inlet_funnel.move_to_position(ipos)
            self._opposing_funnel.move_to_position(opos)
            self.wait_for_move()

    def return_tray_slots(self, tray: Tray, idx: int) -> None:
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        slot_pos = diff - 5
        if tray.cell_width <= 50:
            self._slot_1.move_to_position(slot_pos)
            self._slot_2.move_to_position(slot_pos)
            self._slot_3.move_to_position(0)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            self._slot_1.move_to_position(slot_pos)
            self._slot_2.move_to_position(0)
            self._slot_3.move_to_position(0)
        else:
            self._slot_1.move_to_position(0)
            self._slot_2.move_to_position(0)
            self._slot_3.move_to_position(0)

    def status(self) -> FunnelStatus:
        ipos = self._inlet_funnel.get_position()
        opos = self._opposing_funnel.get_position()
        slot_1 = self._slot_1.get_position()
        slot_2 = self._slot_2.get_position()
        slot_3 = self._slot_3.get_position()
        return FunnelStatus(
            inlet_funnel=ipos,
            opposing_funnel=opos,
            slot_1=slot_1,
            slot_2=slot_2,
            slot_3=slot_3,
        )

    def stop(self) -> None:
        self._inlet_funnel.stop()
        self._opposing_funnel.stop()
        self._slot_1.stop()
        self._slot_2.stop()
        self._slot_3.stop()
        self._belt.stop()
        self._wall.stop()

    def initialize(self) -> None:
        self._inlet_funnel.initialize()
        self._opposing_funnel.initialize()
        self._slot_1.initialize()
        self._slot_2.initialize()
        self._slot_3.initialize()
        self._belt.initialize()
        self._wall.initialize()

    def catch(self) -> None:
        self.inlet_funnel.move_to_position(0)
        self.opposing_funnel.move_to_position(self.offsets.catch_position)
        self.return_slots()

    def open_tray_slots(self, tray: Tray) -> None:
        if tray.cell_width <= 50:
            self.open_slots(1)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            self.open_slots(2)
        else:
            self.open_slots(3)

    def handle_command(self, cmd: FunnelCommands) -> None:
        if cmd.command == Commands.HOME:
            self.home()
        elif cmd.command == Commands.STOP:
            self.stop()
        elif cmd.command == Commands.MOVE_TO_CELL:
            if cmd.payload:
                cell_cmd = MoveToCellCommand(**cmd.payload)
                self.move_to_cell(cell_cmd.tray, cell_cmd.idx)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.CLOSE_FUNNEL:
            slots_open = self.slots_open
            self.close_slots()
            time.sleep(1)
            self.open_slots(slots_open)
        #elif cmd.command == Commands.INLET_COMMAND:
        #    if cmd.payload:
        #        axis_cmd = AxisCommand(**cmd.payload)
        #        self.inlet_funnel.handle_command(axis_cmd)
        #    else:
        #        raise Exception("Invalid Command")
        #elif cmd.command == Commands.OPPOSING_FUNNEL_COMMAND:
        #    if cmd.payload:
        #        axis_cmd = AxisCommand(**cmd.payload)
        #        self.opposing_funnel.handle_command(axis_cmd)
        #    else:
        #        raise Exception("Invalid Command")
        elif cmd.command == Commands.START_BELT:
            self.start_belt()
        elif cmd.command == Commands.STOP_BELT:
            self.stop_belt()
        elif cmd.command == Commands.INITIALIZE:
            self.initialize()
        elif cmd.command == Commands.WIGGLE:
            if cmd.payload:
                wiggle = WiggleCommand(**cmd.payload)
                self.wiggle(wiggle.wiggle)
            else:
                raise Exception("Invalid Command")
        else:
            raise Exception("Invalid Command")
