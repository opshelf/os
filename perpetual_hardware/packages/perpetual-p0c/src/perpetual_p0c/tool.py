from hardware_common.stepper_motor import (
    AbstractStepperMotor,
    StepperMotorConfig,
    Rotation,
)
from enum import Enum
from can import BusABC
from perpetual_trinamic.trinamic_axis import TrinamicAxis
from .io_controller import (
    GenericRelayBoardMessage,
    ToolRelayController,
    RelayBoardMessage,
)
from pydantic import BaseModel
from typing import Optional, Dict, Any
from .tray import Tray
from .common import CanAxisConfiguration, StorageModuleSide, CanMessageIds


class ToolOffsets(BaseModel):
    tool_retract_position: float
    tool_extend_position: float
    tool_safe_min: float
    tool_safe_max: float
    tray_pull_offset: float


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
            tool_configuration.puller.can_id,
            0,
            tool_configuration.puller.configuration,
        )
        self._io = ToolRelayController(bus, can_id=CanMessageIds.TOOL_NODE)
        self._actuators = ToolRelayController(bus, can_id=CanMessageIds.PNEUMATIC_NODE)
        self._actuators_extended = False
        self._carriage_solenoids = 0
        self._puller_solenoids = 0

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
    def io(self) -> ToolRelayController:
        return self._io

    def actuators(self, status: bool) -> None:
        if status:
            self._actuators_extended = True
            self._actuators.set_relays(GenericRelayBoardMessage(r1=True))
        else:
            self._actuators_extended = False
            self._actuators.set_relays(GenericRelayBoardMessage(r1=False))

    def set_puller_solenoids(self, solenoids: int, side: StorageModuleSide) -> None:
        # """
        # if side == "LEFT":
        #    self._io.set_relays(GenericRelayBoardMessage(
        #            r1=False if solenoids > 0 else True,
        #            r2=False if solenoids > 1 else True,
        #            r3=False if solenoids > 2 else True
        #    ))
        # else:
        #    self._io.set_relays(GenericRelayBoardMessage(
        #            r4=False if solenoids > 0 else True,
        #            r5=False if solenoids > 1 else True,
        #            r6=False if solenoids > 2 else True
        #    ))
        # """
        if side == "LEFT":
            self._io.set_relays(
                GenericRelayBoardMessage(
                    r1=True if solenoids > 2 else False,
                    r2=True if solenoids > 1 else False,
                    r3=True if solenoids > 0 else False,
                )
            )
        else:
            self._io.set_relays(
                GenericRelayBoardMessage(
                    r4=True if solenoids > 2 else False,
                    r5=True if solenoids > 1 else False,
                    r6=True if solenoids > 0 else False,
                )
            )

    def set_solenoids_off(self) -> None:
        self._io.set_relays(GenericRelayBoardMessage())

    def set_slot_solenoids(self, slots: int) -> None:
        # self._io.set_relays(GenericRelayBoardMessage(
        #        r7=False if slots > 0 else True,
        #        r8=False if slots > 1 else True,
        #        r9=False if slots > 2 else True,
        #        r10=False if slots > 3 else True
        # ))
        self._io.set_relays(
            GenericRelayBoardMessage(
                r1=True,
                r2=True,
                r3=True,
                r7=True if slots > 0 else False,
                r8=True if slots > 1 else False,
                r9=True if slots > 2 else False,
                r10=True if slots > 3 else False,
            )
        )

    def set_slot_bits(self, bits: int):
        # self._io.set_relays(GenericRelayBoardMessage(
        #        r7=False if bits & 0x01 else True,
        #        r8=False if bits & 0x02 else True,
        #        r9=False if bits & 0x04 else True,
        #        r10=False if bits & 0x08 else True
        # ))
        self._io.set_relays(
            GenericRelayBoardMessage(
                r1=True,
                r2=True,
                r3=True,
                r7=True if bits & 0x01 else False,
                r8=True if bits & 0x02 else False,
                r9=True if bits & 0x04 else False,
                r10=True if bits & 0x08 else False,
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

    def extend_puller(self) -> None:
        self._puller.move_to_position(self.offsets.tool_extend_position)
        self.wait_for_move()

    def retract_puller(self) -> None:
        self._puller.move_to_position(self.offsets.tool_retract_position)
        self.wait_for_move()

    def safe_max(self) -> None:
        self._puller.move_to_position(self.offsets.tool_safe_max)
        self.wait_for_move()

    def safe_min(self) -> None:
        self._puller.move_to_position(self.offsets.tool_safe_min)
        self.wait_for_move()

    def initialize(self) -> None:
        self._io.set_relays(RelayBoardMessage())
        self._io.set_relays(RelayBoardMessage())
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
            self._puller.move_to_position(
                self._puller.configuration.max_travel - self.offsets.tray_pull_offset
            )
        self.wait_for_move()

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
                self.set_puller_solenoids(solenoids.solenoids, StorageModuleSide.LEFT)
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
            self.retract_tray(StorageModuleSide.LEFT)
        elif cmd.command == Commands.EXTEND_PULLER:
            self.extend_puller()
        elif cmd.command == Commands.RETRACT_PULLER:
            self.retract_puller()
        elif cmd.command == Commands.EXTEND_ACTUATORS:
           self.actuators(True) 
        elif cmd.command == Commands.RETRACT_ACTUATORS:
           self.actuators(False) 
        elif cmd.command == Commands.STOP:
           self.stop()
        else: 
            raise Exception("Unknown Tool Command")

