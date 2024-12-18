from hardware_common.stepper_motor import (
    AbstractStepperMotor,
)
from enum import Enum
from can import BusABC
from perpetual_trinamic.trinamic_axis import TrinamicAxis
from pydantic import BaseModel
from typing import Optional, Dict, Any
from perpetual_p0c.storage import Tray, StorageModuleSide
from perpetual_p0c.io_board import ActuatorState, IoController
from perpetual_p0c.tool_board import ToolBoard, IoBoard
from perpetual_p0c.common import CanAxisConfiguration
import time


class ToolOffsets(BaseModel):
    tool_retract_position: float
    tool_extend_position: float
    tool_safe_min: float
    tool_safe_max: float
    tray_pull_offset: float
    tray_pull_right: float
    tray_pull_left: float


class ToolConfiguration(BaseModel):
    puller: CanAxisConfiguration
    offsets: ToolOffsets


class Commands(str, Enum):
    HOME = "HOME"
    INITIALIZE = "INITIALIZE"
    MOVE_Z = "MOVE_Z"
    MOVE_Z_RELATIVE = "MOVE_Z_RELATIVE"
    TURN_ON_PULLER_SOLENOIDS = "TURN_ON_PULLER_SOLENOIDS"
    TURN_OFF_PULLER_SOLENOIDS = "TURN_OFF_PULLER_SOLENOIDS"
    TURN_ON_SLOT_SOLENOIDS = "TURN_ON_SLOT_SOLENOIDS"
    TURN_OFF_SLOT_SOLENOIDS = "TURN_OFF_SLOT_SOLENOIDS"
    RETRACT_TRAY = "RETRACT_TRAY"
    EXTEND_PULLER = "EXTEND_PULLER"
    RETRACT_PULLER = "RETRACT_PULLER"
    EXTEND_ACTUATORS = "EXTEND_ACTUATORS"
    RETRACT_ACTUATORS = "RETRACT_ACTUATORS"
    STOP = "STOP"


class MoveCommand(BaseModel):
    position: float


class TurnOnSolenoids(BaseModel):
    solenoids: int


class ToolCommands(BaseModel):
    command: Commands
    payload: Optional[Dict[Any, Any]]


class ToolStatus(BaseModel):
    puller_solenoids: int
    carriage_solenoids: int
    actuators: bool
    puller_position: float


class Tool:
    def __init__(self, bus: BusABC, tool_configuration: ToolConfiguration):
        self._configuration = tool_configuration
        self._puller = TrinamicAxis(
            bus,
            module_id=tool_configuration.puller.can_id,
            motor_id=0,
            configuration=tool_configuration.puller.configuration,
        )
        self._io = IoController(bus)
        self._tool_board = ToolBoard(bus)
        self._actuators_extended = False
        self._carriage_solenoids = 0
        self._puller_solenoids = 0
        self.set_solenoids_off()
        # atexit.register(self.cleanup)

    def cleanup(self) -> None:
        self._puller.stop()
        self.set_solenoids_off()

    def status(self) -> ToolStatus:
        return ToolStatus(
            puller_solenoids=self._carriage_solenoids,
            carriage_solenoids=self._puller_solenoids,
            actuators=self._actuators_extended,
            puller_position=self._puller.get_position(),
        )

    @property
    def offsets(self) -> ToolOffsets:
        return self._configuration.offsets

    @offsets.setter
    def offsets(self, offsets: ToolOffsets) -> None:
        self._configuration.offsets = offsets

    @property
    def puller(self) -> AbstractStepperMotor:
        return self._puller

    @property
    def io(self) -> IoController:
        return self._io

    @property
    def tool_board(self) -> ToolBoard:
        return self._tool_board

    def actuators(self, status: bool) -> None:
        if status:
            self._actuators_extended = True
            self._io.set_actuators(ActuatorState.Extended)
            t0 = time.time()
            while True:
                if time.time() - t0 > 5:
                    raise Exception("Failed to Extend Actuators")
                forward_actuator = self._tool_board.get_state().inputs.forward_actuator
                time.sleep(0.25)
                if not forward_actuator:
                    break
        else:
            self._actuators_extended = False
            self._io.set_actuators(ActuatorState.Retracted)
            t0 = time.time()
            while True:
                if time.time() - t0 > 5:
                    raise Exception("Failed to Retract Actuators")
                backward_actuator = self._tool_board.get_state().inputs.backward_actuator
                time.sleep(0.25)
                if not backward_actuator:
                    break

    def set_carriage_solenoids(self, solenoids: int, side: StorageModuleSide) -> None:
        if side == "LEFT":
            self._tool_board.set_carriage(
                IoBoard(
                    r1=True if solenoids >= 0 else False,
                    r2=True if solenoids >= 1 else False,
                    r3=True if solenoids >= 2 else False,
                )
            )
        else:
            self._tool_board.set_carriage(
                IoBoard(
                    r4=True if solenoids >= 0 else False,
                    r5=True if solenoids >= 1 else False,
                    r6=True if solenoids >= 2 else False,
                )
            )

    def set_tray_solenoids(self, tray: Tray, side: StorageModuleSide) -> None:
        slots = 1
        if tray.cell_width >= 50 and tray.cell_width < 100:
            slots = 1
        if tray.cell_width >= 100 and tray.cell_width < 150:
            slots = 2
        if tray.cell_width >= 150 and tray.cell_width < 200:
            slots = 3

        if side == "LEFT":
            self._tool_board.set_carriage(
                IoBoard(
                    r1=True if slots >= 1 else False,
                    r2=True if slots >= 2 else False,
                    r3=True if slots >= 3 else False,
                )
            )
        else:
            self._tool_board.set_carriage(
                IoBoard(
                    r4=True if slots >= 1 else False,
                    r5=True if slots >= 2 else False,
                    r6=True if slots >= 3 else False,
                )
            )

    def set_solenoids_off(self) -> None:
        self._tool_board.reset_all()

    def set_slot_bits(self, bits: int) -> None:
        print(bits)
        self._tool_board.set_pullers(
            IoBoard(
                r1=True if bits & 0x01 else False,
                r2=True if bits & 0x02 else False,
                r3=True if bits & 0x04 else False,
                r4=True if bits & 0x08 else False,
                r5=True if bits & 0x10 else False,
                r6=True if bits & 0x20 else False,
            )
        )

    def set_solenoids(self, tray: Tray, index: int) -> None:
        slots = 1
        if tray.cell_width >= 50 and tray.cell_width < 100:
            slots = 1
        if tray.cell_width > 100 and tray.cell_width < 150:
            slots = 2
        if tray.cell_width > 150 and tray.cell_width < 200:
            slots = 3
        start = index * slots
        bits = 0x00
        for i in range(start, start + slots):
            bits |= 0x01 << i
        self.set_slot_bits(bits)

    def extend_puller(self, side: StorageModuleSide = StorageModuleSide.RIGHT) -> None:
        # self._puller.move_to_position(self.offsets.tool_extend_position)
        if side == StorageModuleSide.RIGHT:
            self._puller.move_to_position(self.offsets.tool_extend_position)
            self.wait_for_move()
        else:
            self._puller.move_to_position(0)
            self.wait_for_move()

    def retract_puller(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.RIGHT:
            self._puller.move_to_position(self.offsets.tray_pull_right)
            self.wait_for_move()
        else:
            self._puller.move_to_position(self.offsets.tray_pull_left)
            self.wait_for_move()
        # self._puller.move_to_position(self.offsets.tool_retract_position)
        # self.wait_for_move()

    def puller_safe(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.RIGHT:
            self.safe_max()
        else:
            self.safe_min()

    def safe_max(self) -> None:
        self._puller.move_to_position(self.offsets.tool_safe_max)
        self.wait_for_move()

    def safe_min(self) -> None:
        self._puller.move_to_position(self.offsets.tool_safe_min)
        self.wait_for_move()

    def initialize(self) -> None:
        self._io.reset_valves()
        self._tool_board.reset_all()
        self._puller.initialize()

    def home(self) -> None:
        self._puller.home()

    def wait_for_move(self) -> None:
        self._puller.wait_for_move()

    def stop(self) -> None:
        self._puller.stop()

    def retract_tray(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.LEFT:
            self._puller.move_to_position(self.offsets.tray_pull_offset)
        else:
            if self._puller.configuration.max_travel:
                self._puller.move_to_position(
                    self._puller.configuration.max_travel - self.offsets.tray_pull_offset
                )
        self.wait_for_move()

    def check_tray_clear(self) -> bool:
        state = self._tool_board.get_state()
        return state.inputs.left_beam and state.inputs.right_beam

    def check_tray_loaded(self) -> bool:
        state = self._tool_board.get_state()
        return (
            state.inputs.left_beam
            and state.inputs.right_beam
            and not state.inputs.left_presence
            and not state.inputs.right_presence
        )

    def get_max(self) -> float:
        self._puller.home()
        self._puller.move_to_position(1000)
        try:
            self._puller.wait_for_move()
        except Exception as _:
            pass
        position = self._puller.get_position()
        self._puller.home()
        return position

    def drop_items(self, tray: Tray, idx: int) -> None:
        if tray.cell_width <= 50.0:
            position = (tray.cells - 1) - idx
            self._tool_board.set_pullers(IoBoard.from_buffer(1 << position))
            self.actuators(True)
        elif tray.cell_width <= 105.0:
            position = (tray.cells - 1) - idx
            positions = 1 << (position * 2)
            positions |= 1 << (position * 2 + 1)
            self._tool_board.set_pullers(IoBoard.from_buffer(positions))
            self.actuators(True)
        else:
            position = (tray.cells - 1) - idx
            positions = 1 << (position * 3)
            positions |= 1 << (position * 3 + 1)
            positions |= 1 << (position * 3 + 2)
            self._tool_board.set_pullers(IoBoard.from_buffer(positions))
            self.actuators(True)

    def handle_command(self, cmd: ToolCommands) -> None:
        if cmd.command == Commands.HOME:
            self.home()
        elif cmd.command == Commands.INITIALIZE:
            self.initialize()
        elif cmd.command == Commands.MOVE_Z:
            if cmd.payload:
                move = MoveCommand(**cmd.payload)
                self._puller.move_to_position(move.position)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.MOVE_Z_RELATIVE:
            if cmd.payload:
                move = MoveCommand(**cmd.payload)
                self._puller.move_to_relative_position(move.position)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.TURN_ON_PULLER_SOLENOIDS:
            if cmd.payload:
                solenoids = TurnOnSolenoids(**cmd.payload)
                self.set_carriage_solenoids(solenoids.solenoids, StorageModuleSide.LEFT)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.TURN_OFF_PULLER_SOLENOIDS:
            self.set_solenoids_off()
        elif cmd.command == Commands.TURN_ON_SLOT_SOLENOIDS:
            if cmd.payload:
                solenoids = TurnOnSolenoids(**cmd.payload)
                self.set_slot_bits(solenoids.solenoids)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.TURN_OFF_SLOT_SOLENOIDS:
            self.set_solenoids_off()
        elif cmd.command == Commands.RETRACT_TRAY:
            self.retract_tray(StorageModuleSide.RIGHT)
        elif cmd.command == Commands.EXTEND_PULLER:
            self.extend_puller()
        elif cmd.command == Commands.RETRACT_PULLER:
            self.retract_puller(StorageModuleSide.RIGHT)
        elif cmd.command == Commands.EXTEND_ACTUATORS:
            self.actuators(True)
        elif cmd.command == Commands.RETRACT_ACTUATORS:
            self.actuators(False)
        elif cmd.command == Commands.STOP:
            self.stop()
        else:
            raise Exception("Unknown Tool Command")
