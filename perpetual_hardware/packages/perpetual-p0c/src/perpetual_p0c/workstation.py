from perpetual_can import CANBusHandler
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis, AxisStatus
from perpetual_p0c.output_box import OutputBox, OutputBoxStatus
from typing import Literal, Union, Annotated
from pydantic import BaseModel, Field
from perpetual_p0c.common import AbstractSubsystem, CanAxisConfiguration
from perpetual_p0c.output_box import OutputBoxConfiguration


class WorkstationConfiguration(BaseModel):
    outputbox_configuration: OutputBoxConfiguration
    input_belt_configuration: CanAxisConfiguration
    output_belt_configuration: CanAxisConfiguration
    input_belt_speed: int
    output_belt_speed: int


class WorkstationStatus(BaseModel):
    output_box: OutputBoxStatus
    input_belt: AxisStatus
    output_belt: AxisStatus


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


class CloseOutputCommand(BaseModel):
    command: Literal["close_output"]


class HomeOutput(BaseModel):
    command: Literal["home_output"]


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
        self._output_box = OutputBox(bus, self._configuration.outputbox_configuration)
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
        await self._output_box.stop()

    async def home(self) -> None:
        await self.home_output()
        await self.input_belt.initialize()
        await self.output_belt.initialize()

    async def initialize(self) -> None:
        await self.output_box.initialize()
        await self.input_belt.initialize()
        await self.output_belt.initialize()

    @property
    def configuration(self) -> WorkstationConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: WorkstationConfiguration) -> None:
        self._configuration = configuration
        self._output_belt.configuration = (
            self._configuration.output_belt_configuration.configuration
        )
        self._input_belt.configuration = self._configuration.input_belt_configuration.configuration
        await self._output_box.set_configuration(self._configuration.outputbox_configuration)
        await self.initialize()

    async def status(self) -> WorkstationStatus:
        output_box_status = await self.output_box.status()
        return WorkstationStatus(output_box=output_box_status, input_belt=await self.input_belt.status(), output_belt=await self.output_belt.status())

    @property
    def output_box(self) -> OutputBox:
        return self._output_box

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

    async def open_output(self) -> None:
        await self.output_box.open()

    async def close_output(self) -> None:
        await self.output_box.close()

    async def home_output(self) -> None:
        await self.output_box.home()

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
            case OpenOutputCommand():
                await self.open_output()
            case CloseOutputCommand():
                await self.close_output()
            case HomeOutput():
                await self.home_output()
            case _:
                raise Exception("Invalid Workstation Command")
