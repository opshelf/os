from perpetual_can import CANBusHandler
from pydantic import BaseModel, Field
from pymodbus.client.serial import AsyncModbusSerialClient
#from perpetual_os1.gripper import GripperConfiguration, Gripper
from perpetual_os1.gripper.homwing import HomwingGripper
from perpetual_os1.common import CanAxisConfiguration, AbstractSubsystem
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from pydantic import BaseModel
from typing import Literal, Annotated, Union

from perpetual_os1.gripper.homwing.core import ExecuteControlMovement

class GripperConfiguration(BaseModel):
    min_width: float = 20.0
    max_width: float = 40.0
    gripper_pickup_width: float = 30.0


class ToolConfiguration(BaseModel):
    gripper: GripperConfiguration
    z_axis: CanAxisConfiguration
    tool_shelf_offset: float
    tool_retracted_offset: float
    tool_extension_offset: float


class ToolStatus(BaseModel):
    #gripper: AxisStatus
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
    def __init__(self, bus: CANBusHandler, client: AsyncModbusSerialClient,configuration: ToolConfiguration) -> None:
        self._configuration = configuration
        #self._gripper = Gripper(bus, self._configuration.gripper)
        self._gripper = HomwingGripper(client, 9)
        self._z_axis = TrinamicAxis(
            bus,
            configuration=self._configuration.z_axis.configuration,
            module_id=self._configuration.z_axis.can_id,
        )

    @property
    def z_axis(self) -> TrinamicAxis:
        return self._z_axis

    @property
    def gripper(self) -> HomwingGripper:
        return self._gripper

    @property
    def configuration(self) -> ToolConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: ToolConfiguration) -> None:
        self._configuration = configuration
        self._z_axis.configuration = self._configuration.z_axis.configuration
        await self.initialize()

    async def status(self) -> ToolStatus:
        z_axis = await self._z_axis.status()
        #gripper = await self._gripper.axis.status()
        #return ToolStatus(gripper=gripper, z_axis=z_axis)
        return ToolStatus(z_axis=z_axis)

    async def stop(self) -> None:
        await self._z_axis.stop()

    async def home(self) -> None:
        await self._z_axis.home()
        await self._gripper.activate()
        await self._gripper.wait_for_activation()

    async def initialize(self) -> None:
        await self._z_axis.initialize()
        await self._gripper.activate()
        await self._gripper.wait_for_activation()

    async def set_jaw_size(self, width: float) -> None:
        value = int((1 - ((width - self._configuration.gripper.min_width)/(self._configuration.gripper.max_width-self._configuration.gripper.min_width))) * 255.0)
        await self._gripper.execute_control_movement(ExecuteControlMovement(
            speed=255,
            force=255,
            position=max(0,min(
                value,
                255
            ))
        ))

    async def setup_box_pick(self) -> None:
        #await self.set_jaw_size(width + self._configuration.gripper.gap)
        await self.set_jaw_size(28.0)

    async def setup_box_grab(self) -> None:
        #await self.set_jaw_size(width - self._configuration.gripper.compression)
        await self.set_jaw_size(40.0)

    async def setup_box_drop(self) -> None:
        #await self.set_jaw_size(width + self._configuration.gripper.gap)
        await self.set_jaw_size(20.0)

    async def grab_box(self, width: float) -> None:
        await self.setup_box_pick()
        await self.extend()
        await self.setup_box_grab()
        await self.z_axis.move_to_position(self._configuration.tool_retracted_offset)
        await self.z_axis.wait_for_move()

    async def extend_box(self, width: float) -> None:
        await self.z_axis.move_to_position(width + self._configuration.tool_shelf_offset)
        await self.z_axis.wait_for_move()

    async def retract_z(self) -> None:
        await self.z_axis.move_to_position(self._configuration.tool_retracted_offset)
        await self.z_axis.wait_for_move()

    async def place_box(self, width: float) -> None:
        await self.setup_box_drop()
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
                await self.set_jaw_size(size)
                return ToolCommandSuccessful()
            case _:
                raise Exception("Invalid Tool Command")
