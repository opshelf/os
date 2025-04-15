from perpetual_os1.common import CanAxisConfiguration, AbstractSubsystem
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from pydantic import BaseModel
from perpetual_can import CANBusHandler


class GripperConfiguration(BaseModel):
    axis: CanAxisConfiguration
    minimum_gripper_width: float
    maximum_gripper_width: float
    compression: float
    gap: float


class GripperStatus(BaseModel):
    axis: AxisStatus

class Gripper(AbstractSubsystem[GripperConfiguration, GripperStatus]):
    def __init__(self, bus: CANBusHandler, configuration: GripperConfiguration) -> None:
        self._configuration = configuration
        self._axis = TrinamicAxis(
            bus,
            configuration=self._configuration.axis.configuration,
            module_id=self._configuration.axis.can_id,
        )

    @property
    def axis(self) -> TrinamicAxis:
        return self._axis

    async def status(self) -> GripperStatus:
        status = await self._axis.status()
        return GripperStatus(
            axis=status
        )

    async def stop(self) -> None:
        await self._axis.stop()

    @property
    def configuration(self) -> GripperConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: GripperConfiguration) -> None:
        self._configuration = configuration
        self._axis.configuration = self._configuration.axis.configuration
        await self.initialize()

    async def initialize(self) -> None:
        await self._axis.initialize()
        await self._axis.set_pullup_resistors(0)

    async def home(self) -> None:
        await self._axis.home()

    async def close_jaw(self) -> None:
        try:
            await self._axis.move_to_position(0)
            await self._axis.wait_for_move()
        except Exception as e:
            await self._axis.initialize()

    async def set_jaw_size(self, size: float) -> None:
        gap = self._configuration.minimum_gripper_width
        target_size = size - gap
        jaw_position = target_size / 2
        await self._axis.move_to_position(jaw_position)
        await self._axis.wait_for_move()
