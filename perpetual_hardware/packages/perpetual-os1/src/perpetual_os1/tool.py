from perpetual_can import CANBusHandler
from pydantic import BaseModel, Field
from perpetual_os1.gripper import GripperConfiguration, Gripper
from perpetual_os1.common import CanAxisConfiguration, AbstractSubsystem
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from pydantic import BaseModel
from typing import Literal, Annotated, Union


class ToolConfiguration(BaseModel):
    gripper: GripperConfiguration
    z_axis: CanAxisConfiguration
    tool_shelf_offset: float
    tool_retracted_offset: float
    tool_extension_offset: float


class ToolStatus(BaseModel):
    gripper: AxisStatus
    z_axis: AxisStatus


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class MoveCommand(BaseModel):
    command: Literal["move"]
    position: float


class MoveRelativeCommand(BaseModel):
    command: Literal["move_relative"]
    position: float


class SetJawSizeCommand(BaseModel):
    command: Literal["set_jaw_size"]
    size: float


ToolCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveRelativeCommand,
        SetJawSizeCommand,
    ],
    Field(discriminator="command"),
]


class ToolCommandSuccessful(BaseModel):
    message: Literal["success"] = "success"


class ToolCommandError(BaseModel):
    error: str


ToolResponse = Union[ToolCommandSuccessful, ToolCommandError]


class Tool(AbstractSubsystem[ToolConfiguration, ToolStatus]):
    def __init__(self, bus: CANBusHandler, configuration: ToolConfiguration) -> None:
        self._configuration = configuration
        self._gripper = Gripper(bus, self._configuration.gripper)
        self._z_axis = TrinamicAxis(
            bus,
            configuration=self._configuration.z_axis.configuration,
            module_id=self._configuration.z_axis.can_id,
        )

    @property
    def z_axis(self) -> TrinamicAxis:
        return self._z_axis

    @property
    def gripper(self) -> Gripper:
        return self._gripper

    @property
    def configuration(self) -> ToolConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: ToolConfiguration) -> None:
        self._configuration = configuration
        await self._gripper.set_configuration(self._configuration.gripper)
        self._z_axis.configuration = self._configuration.z_axis.configuration
        await self.initialize()

    async def status(self) -> ToolStatus:
        z_axis = await self._z_axis.status()
        gripper = await self._gripper.axis.status()
        return ToolStatus(gripper=gripper, z_axis=z_axis)

    async def stop(self) -> None:
        await self._z_axis.stop()
        await self._gripper.stop()

    async def home(self) -> None:
        await self._z_axis.home()
        await self._gripper.home()

    async def initialize(self) -> None:
        await self._z_axis.initialize()
        await self._gripper.initialize()

    async def setup_box_pick(self, width: float) -> None:
        try:
            await self.gripper.set_jaw_size(width + self._configuration.gripper.gap)
        except Exception as e:
            await self.gripper.initialize()

    async def setup_box_grab(self, width: float) -> None:
        await self.gripper.axis.safety_off()
        await self.gripper.set_jaw_size(width - self._configuration.gripper.compression)
        #await self.gripper.close_jaw()
        await self.gripper.initialize()

    async def setup_box_drop(self, width: float) -> None:
        await self.gripper.set_jaw_size(width + self._configuration.gripper.gap)

    async def grab_box(self, width: float) -> None:
        await self.setup_box_pick(width)
        await self.extend()
        await self.setup_box_grab(width)
        await self.z_axis.move_to_position(self._configuration.tool_retracted_offset)
        await self.z_axis.wait_for_move()

    async def extend_box(self, width: float) -> None:
        await self.z_axis.move_to_position(width + self._configuration.tool_shelf_offset)
        await self.z_axis.wait_for_move()

    async def retract_z(self) -> None:
        await self.z_axis.move_to_position(self._configuration.tool_retracted_offset)
        await self.z_axis.wait_for_move()

    async def place_box(self, width: float) -> None:
        await self.setup_box_drop(width)
        await self.extend()
        await self.retract_z()

    async def extend(self) -> None:
        await self.z_axis.move_to_position(self._configuration.tool_extension_offset)
        await self.z_axis.wait_for_move()

    async def handle_command(self, command: ToolCommands) -> ToolResponse:
        match command:
            case StopCommand():
                await self.stop()
                return ToolCommandSuccessful()
            case InitializeCommand():
                await self.initialize()
                return ToolCommandSuccessful()
            case HomeCommand():
                await self.home()
                return ToolCommandSuccessful()
            case MoveCommand(position=position):
                await self._z_axis.move_to_position(position)
                return ToolCommandSuccessful()
            case MoveRelativeCommand(position=position):
                await self._z_axis.move_to_relative_position(position)
                return ToolCommandSuccessful()
            case SetJawSizeCommand(size=size):
                await self._gripper.set_jaw_size(size)
                return ToolCommandSuccessful()
            case _:
                raise Exception("Invalid Tool Command")
