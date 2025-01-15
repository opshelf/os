import asyncio
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from perpetual_can import CANBusHandler
from perpetual_p0c.storage import Tray, StorageModuleSide
from perpetual_p0c.common import CanAxisConfiguration, AbstractSubsystem
from perpetual_p0c.io_board import ActuatorState, IoController
from perpetual_p0c.tool_board import ToolBoard, IoBoard
from typing import Literal, Union, Annotated
from pydantic import BaseModel, Field


class ToolConfiguration(BaseModel):
    carriage: CanAxisConfiguration
    tool_retract_position: float
    tool_extend_position: float
    tool_safe_min: float
    tool_safe_max: float
    tray_pull_offset: float
    tray_pull_right: float
    tray_pull_left: float


class ToolStatus(BaseModel):
    puller_solenoids: int
    carriage_solenoids: int
    actuators: bool
    carriage_status: AxisStatus


class ShutdownCommand(BaseModel):
    command: Literal["shutdown"]


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class ActuatorsCommand(BaseModel):
    command: Literal["actuators"]
    value: bool


class CarriageSolenoidsCommand(BaseModel):
    command: Literal["set_carriage_solenoids"]
    tray: Tray
    side: StorageModuleSide


class PullerSolenoidsCommand(BaseModel):
    command: Literal["set_puller_solenoids"]
    tray: Tray
    idx: int


class SolenoidsOffCommand(BaseModel):
    command: Literal["solenoids_off"]


class SetSlotBitsCommand(BaseModel):
    command: Literal["slot_bits"]
    value: int


class ExtendCarriageCommand(BaseModel):
    command: Literal["extend_carriage"]
    side: StorageModuleSide


class RetractCarriageCommand(BaseModel):
    command: Literal["retract_carriage"]
    side: StorageModuleSide


class RetractTrayCommand(BaseModel):
    command: Literal["retract_tray"]
    side: StorageModuleSide


class DropItemsCommand(BaseModel):
    command: Literal["drop_items"]
    tray: Tray
    idx: int


class HomeToolCommand(BaseModel):
    command: Literal["home"]


class CarriageMoveAbsolute(BaseModel):
    command: Literal["carriage_absolute"]
    position: float


class CarriageMoveRelative(BaseModel):
    command: Literal["carriage_relative"]
    position: float


ToolCommands = Annotated[
    Union[
        ShutdownCommand,
        StopCommand,
        InitializeCommand,
        ActuatorsCommand,
        CarriageSolenoidsCommand,
        PullerSolenoidsCommand,
        SolenoidsOffCommand,
        SetSlotBitsCommand,
        ExtendCarriageCommand,
        RetractCarriageCommand,
        RetractTrayCommand,
        DropItemsCommand,
        HomeToolCommand,
        CarriageMoveAbsolute,
        CarriageMoveRelative,
    ],
    Field(discriminator="command"),
]


class Tool(AbstractSubsystem[ToolConfiguration, ToolStatus]):
    def __init__(self, bus: CANBusHandler, tool_configuration: ToolConfiguration, io: IoController):
        self._configuration = tool_configuration
        self._carriage = TrinamicAxis(
            bus,
            module_id=tool_configuration.carriage.can_id,
            motor_id=0,
            configuration=tool_configuration.carriage.configuration,
        )
        self._io = io
        self._tool_board = ToolBoard(bus)
        self._actuators_extended = False
        self._carriage_solenoids = 0
        self._carriage_solenoids = 0

    async def cleanup(self) -> None:
        await self._carriage.stop()
        await self.set_solenoids_off()

    async def status(self) -> ToolStatus:
        carriage_status = await self._carriage.status()
        state = await self._tool_board.get_state()
        return ToolStatus(
            puller_solenoids=state.puller.to_int(),
            carriage_solenoids=state.carriage.to_int(),
            actuators=not state.inputs.forward_actuator,
            carriage_status=carriage_status,
        )

    @property
    def configuration(self) -> ToolConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: ToolConfiguration) -> None:
        self._configuration = configuration
        self._carriage.configuration = configuration.carriage.configuration
        await self._carriage.initialize()

    @property
    def carriage(self) -> TrinamicAxis:
        return self._carriage

    @property
    def io(self) -> IoController:
        return self._io

    @property
    def tool_board(self) -> ToolBoard:
        return self._tool_board

    async def actuators(self, status: bool) -> None:
        if status:
            await self._io.set_actuators(ActuatorState.Extended)
            async with asyncio.timeout(5):
                while True:
                    state = await self._tool_board.get_state()
                    if not state.inputs.forward_actuator:
                        break
                    await asyncio.sleep(0.25)
            self._actuators_extended = True
        else:
            await self._io.set_actuators(ActuatorState.Retracted)
            async with asyncio.timeout(5):
                while True:
                    state = await self._tool_board.get_state()
                    if not state.inputs.backward_actuator:
                        break
                    await asyncio.sleep(0.25)
            self._actuators_extended = False

    async def set_carriage_solenoids(self, solenoids: int, side: StorageModuleSide) -> None:
        if side == "LEFT":
            await self._tool_board.set_carriage(
                IoBoard(
                    r1=True if solenoids >= 0 else False,
                    r2=True if solenoids >= 1 else False,
                    r3=True if solenoids >= 2 else False,
                )
            )
        else:
            await self._tool_board.set_carriage(
                IoBoard(
                    r4=True if solenoids >= 0 else False,
                    r5=True if solenoids >= 1 else False,
                    r6=True if solenoids >= 2 else False,
                )
            )

    async def set_tray_solenoids(self, tray: Tray, side: StorageModuleSide) -> None:
        slots = 1
        if tray.cell_width >= 50 and tray.cell_width < 100:
            slots = 1
        if tray.cell_width >= 100 and tray.cell_width < 150:
            slots = 2
        if tray.cell_width >= 150 and tray.cell_width < 200:
            slots = 3

        if side == "LEFT":
            await self._tool_board.set_carriage(
                IoBoard(
                    r1=True if slots >= 1 else False,
                    r2=True if slots >= 2 else False,
                    r3=True if slots >= 3 else False,
                )
            )
        else:
            await self._tool_board.set_carriage(
                IoBoard(
                    r4=True if slots >= 1 else False,
                    r5=True if slots >= 2 else False,
                    r6=True if slots >= 3 else False,
                )
            )

    async def set_solenoids_off(self) -> None:
        await self._tool_board.reset_all()

    async def set_slot_bits(self, bits: int) -> None:
        await self._tool_board.set_pullers(
            IoBoard(
                r1=True if bits & 0x01 else False,
                r2=True if bits & 0x02 else False,
                r3=True if bits & 0x04 else False,
                r4=True if bits & 0x08 else False,
                r5=True if bits & 0x10 else False,
                r6=True if bits & 0x20 else False,
            )
        )

    async def set_solenoids(self, tray: Tray, index: int) -> None:
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
        await self.set_slot_bits(bits)

    async def extend_carriage(self, side: StorageModuleSide = StorageModuleSide.RIGHT) -> None:
        if side == StorageModuleSide.RIGHT:
            await self._carriage.move_to_position(self._configuration.tool_extend_position)
            await self.wait_for_move()
        else:
            await self._carriage.move_to_position(0)
            await self.wait_for_move()

    async def retract_carriage(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.RIGHT:
            await self._carriage.move_to_position(self._configuration.tray_pull_right)
            await self.wait_for_move()
        else:
            await self._carriage.move_to_position(self._configuration.tray_pull_left)
            await self.wait_for_move()

    async def carriage_safe(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.RIGHT:
            await self.safe_max()
        else:
            await self.safe_min()

    async def safe_max(self) -> None:
        await self._carriage.move_to_position(self._configuration.tool_safe_max)
        await self.wait_for_move()

    async def safe_min(self) -> None:
        await self._carriage.move_to_position(self._configuration.tool_safe_min)
        await self.wait_for_move()

    async def initialize(self) -> None:
        await self._io.reset_valves()
        await self._tool_board.reset_all()
        await self._carriage.initialize()

    async def home(self) -> None:
        await self._carriage.home()

    async def wait_for_move(self) -> None:
        await self._carriage.wait_for_move()

    async def stop(self) -> None:
        await self._carriage.stop()

    async def retract_tray(self, side: StorageModuleSide) -> None:
        if side == StorageModuleSide.LEFT:
            await self._carriage.move_to_position(self._configuration.tray_pull_offset)
        else:
            if self._carriage.configuration.max_travel:
                await self._carriage.move_to_position(
                    self._carriage.configuration.max_travel - self._configuration.tray_pull_offset
                )
        await self.wait_for_move()

    async def check_tray_clear(self) -> bool:
        state = await self._tool_board.get_state()
        return state.inputs.left_beam and state.inputs.right_beam

    async def check_tray_loaded(self) -> bool:
        state = await self._tool_board.get_state()
        return (
            state.inputs.left_beam
            and state.inputs.right_beam
            and not state.inputs.left_presence
            and not state.inputs.right_presence
        )

    async def get_max(self) -> float:
        await self._carriage.home()
        await self._carriage.move_to_position(1000)
        try:
            await self._carriage.wait_for_move()
        except Exception as _:
            pass
        position = await self._carriage.get_position()
        await self._carriage.home()
        return position

    async def drop_items(self, tray: Tray, idx: int) -> None:
        if tray.cell_width <= 50.0:
            position = (tray.cells - 1) - idx
            await self._tool_board.set_pullers(IoBoard.from_buffer(1 << position))
            await self.actuators(True)
        elif tray.cell_width <= 105.0:
            position = (tray.cells - 1) - idx
            positions = 1 << (position * 2)
            positions |= 1 << (position * 2 + 1)
            await self._tool_board.set_pullers(IoBoard.from_buffer(positions))
            await self.actuators(True)
        else:
            position = (tray.cells - 1) - idx
            positions = 1 << (position * 3)
            positions |= 1 << (position * 3 + 1)
            positions |= 1 << (position * 3 + 2)
            await self._tool_board.set_pullers(IoBoard.from_buffer(positions))
            await self.actuators(True)

    async def handle_command(self, command: ToolCommands) -> None:
        match command:
            case ShutdownCommand():
                await self.cleanup()
            case StopCommand():
                await self.stop()
            case InitializeCommand():
                await self.initialize()
            case ActuatorsCommand(value=value):
                await self.actuators(value)
            case CarriageSolenoidsCommand(tray=tray, side=side):
                await self.set_tray_solenoids(tray, side)
            case PullerSolenoidsCommand(tray=tray, idx=idx):
                await self.set_solenoids(tray, idx)
            case SolenoidsOffCommand():
                await self.set_solenoids_off()
            case SetSlotBitsCommand(value=bits):
                await self.set_slot_bits(bits)
            case ExtendCarriageCommand(side=side):
                await self.extend_carriage(side)
            case RetractCarriageCommand(side=side):
                await self.retract_carriage(side)
            case RetractTrayCommand(side=side):
                await self.retract_tray(side)
            case DropItemsCommand(tray=tray, idx=idx):
                await self.drop_items(tray, idx)
            case HomeToolCommand():
                await self.home()
            case CarriageMoveAbsolute(position=position):
                await self._carriage.move_to_position(position)
            case CarriageMoveRelative(position=position):
                await self._carriage.move_to_relative_position(position)
            case _:
                raise Exception("Invalid Tool Command")
