from perpetual_can import CANBusHandler
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis, AxisStatus
from perpetual_p0c.output_box import OutputBox, OutputBoxStatus
from typing import Literal, Union, Annotated
from pydantic import BaseModel, Field
from perpetual_p0c.common import AbstractSubsystem, CanAxisConfiguration
from perpetual_p0c.output_box import OutputBoxConfiguration

class WorkstationConfiguration(BaseModel):
    outputbox_1_configuration: OutputBoxConfiguration
    outputbox_2_configuration: OutputBoxConfiguration
    outputbox_3_configuration: OutputBoxConfiguration
    input_belt_configuration: CanAxisConfiguration
    output_belt_configuration: CanAxisConfiguration
    input_belt_speed: int
    output_belt_speed: int


class WorkstationStatus(BaseModel):
    output_box_1: OutputBoxStatus
    output_box_2: OutputBoxStatus
    output_box_3: OutputBoxStatus
    input_belt: AxisStatus
    output_belt: AxisStatus


class OutputBoxIdentifier(BaseModel):
    output_box_id: int


class ShutdownCommand(BaseModel):
    command: Literal["shutdown"]


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class StartInputCommand(BaseModel):
    command: Literal["start_input_belt"]


class StopInputCommand(BaseModel):
    command: Literal["stop_input_belt"]


class StartOutputCommand(BaseModel):
    command: Literal["start_output_belt"]


class StopOutputCommand(BaseModel):
    command: Literal["stop_output_belt"]


class OpenOutputCommand(BaseModel):
    command: Literal["open_output"]
    payload: OutputBoxIdentifier


class CloseOutputCommand(BaseModel):
    command: Literal["close_output"]
    payload: OutputBoxIdentifier


class HomeOutput(BaseModel):
    command: Literal["home_output"]
    payload: OutputBoxIdentifier


WorkstationCommands = Annotated[
    Union[
        ShutdownCommand,
        StopCommand,
        InitializeCommand,
        StartInputCommand,
        StopInputCommand,
        StartOutputCommand,
        StopOutputCommand,
        OpenOutputCommand,
        CloseOutputCommand,
        HomeOutput,
    ],
    Field(discriminator="command"),
]


class Workstation(AbstractSubsystem[WorkstationConfiguration, WorkstationStatus]):
    def __init__(self, bus: CANBusHandler, configuration: WorkstationConfiguration):
        self._configuration = configuration
        self._output_box_1 = OutputBox(bus, self._configuration.outputbox_1_configuration)
        self._output_box_2 = OutputBox(bus, self._configuration.outputbox_2_configuration)
        self._output_box_3 = OutputBox(bus, self._configuration.outputbox_3_configuration)
        self._input_belt = TrinamicAxis(
            bus,
            module_id=self._configuration.input_belt_configuration.can_id,
            motor_id=0,
            configuration=self._configuration.input_belt_configuration.configuration,
            max_current=6000,
        )
        self._output_belt = TrinamicAxis(
            bus,
            module_id=self._configuration.output_belt_configuration.can_id,
            motor_id=0,
            configuration=self._configuration.output_belt_configuration.configuration,
            max_current=6000,
        )

    async def cleanup(self) -> None:
        await self._output_belt.stop()
        await self._input_belt.stop()

    async def stop(self) -> None:
        await self._output_belt.stop()
        await self._input_belt.stop()
        await self._output_box_1.stop()
        await self._output_box_2.stop()
        await self._output_box_3.stop()

    async def home(self) -> None:
        await self._output_box_1.home()
        await self._output_box_2.home()
        await self._output_box_3.home()
        await self._input_belt.initialize()
        await self._output_belt.initialize()

    async def initialize(self) -> None:
        await self._output_box_1.initialize()
        await self._output_box_2.initialize()
        await self._output_box_3.initialize()
        await self._input_belt.initialize()
        await self._output_belt.initialize()

    @property
    def configuration(self) -> WorkstationConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: WorkstationConfiguration) -> None:
        self._configuration = configuration
        self._output_belt.configuration = (
            self._configuration.output_belt_configuration.configuration
        )
        self._input_belt.configuration = self._configuration.input_belt_configuration.configuration
        await self._output_box_1.set_configuration(self._configuration.outputbox_1_configuration)
        await self._output_box_2.set_configuration(self._configuration.outputbox_2_configuration)
        await self._output_box_3.set_configuration(self._configuration.outputbox_3_configuration)
        await self.initialize()

    async def status(self) -> WorkstationStatus:
        output_box_1_status = await self._output_box_1.status()
        output_box_2_status = await self._output_box_2.status()
        output_box_3_status = await self._output_box_3.status()
        return WorkstationStatus(
            output_box_1=output_box_1_status,
            output_box_2=output_box_2_status,
            output_box_3=output_box_3_status,
            input_belt=await self.input_belt.status(),
            output_belt=await self.output_belt.status(),
        )

    async def setup_output_boxes(self, idx: int) -> None:
        if idx == 0:
            await self._output_box_1.open()
            await self._output_box_2.close()
            await self._output_box_3.close()
        elif idx == 1:
            await self._output_box_1.close()
            await self._output_box_2.open()
            await self._output_box_3.close()
        elif idx == 2:
            await self._output_box_1.close()
            await self._output_box_2.close()
            await self._output_box_3.open()
        else:
            raise Exception("Invalid Output Box")

    @property
    def output_box_1(self) -> OutputBox:
        return self._output_box_1

    @property
    def output_box_2(self) -> OutputBox:
        return self._output_box_2

    @property
    def output_box_3(self) -> OutputBox:
        return self._output_box_3

    @property
    def input_belt(self) -> TrinamicAxis:
        return self._input_belt

    @property
    def output_belt(self) -> TrinamicAxis:
        return self._output_belt

    async def start_input_belt(self) -> None:
        await self.input_belt.initialize()
        await self.input_belt.rotate(self._configuration.input_belt_speed)

    async def stop_input_belt(self) -> None:
        await self.input_belt.stop()

    async def start_output_belt(self) -> None:
        await self.output_belt.initialize()
        await self.output_belt.rotate(self._configuration.output_belt_speed)

    async def stop_output_belt(self) -> None:
        await self.output_belt.stop()

    async def open_output(self, idx: int = 0) -> None:
        if idx == 0:
            await self._output_box_1.open()
            await self._output_box_2.open()
            await self._output_box_3.open()
        elif idx == 1:
            await self._output_box_1.open()
        elif idx == 2:
            await self._output_box_2.open()
        elif idx == 3:
            await self._output_box_3.open()
        else:
            raise Exception("Invalid Output Box")

    async def close_output(self, idx: int = 0) -> None:
        if idx == 0:
            await self._output_box_1.open()
            await self._output_box_2.open()
            await self._output_box_3.open()
        elif idx == 1:
            await self._output_box_1.close()
        elif idx == 2:
            await self._output_box_2.close()
        elif idx == 3:
            await self._output_box_3.close()
        else:
            raise Exception("Invalid Output Box")

    async def home_output(self, idx: int = 0) -> None:
        if idx == 0:
            await self._output_box_1.home()
            await self._output_box_2.home()
            await self._output_box_3.home()
        elif idx == 1:
            await self._output_box_1.home()
        elif idx == 2:
            await self._output_box_2.home()
        elif idx == 3:
            await self._output_box_3.home()
        else:
            raise Exception("Invalid Output Box")

    async def handle_command(self, command: WorkstationCommands) -> None:
        match command:
            case ShutdownCommand():
                await self.cleanup()
            case StopCommand():
                await self.stop()
            case InitializeCommand():
                await self.initialize()
            case StartInputCommand():
                await self.start_input_belt()
            case StopInputCommand():
                await self.stop_input_belt()
            case StartOutputCommand():
                await self.start_output_belt()
            case StopOutputCommand():
                await self.stop_output_belt()
            case OpenOutputCommand(payload=payload):
                await self.open_output(payload.output_box_id)
            case CloseOutputCommand(payload=payload):
                await self.close_output(payload.output_box_id)
            case HomeOutput(payload=payload):
                await self.home_output(payload.output_box_id)
            case _:
                raise Exception("Invalid Workstation Command")
