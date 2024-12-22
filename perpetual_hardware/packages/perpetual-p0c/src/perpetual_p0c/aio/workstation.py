from perpetual_can import CANBusHandler
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_p0c.aio.output_box import OutputBox
from perpetual_p0c.workstation import WorkstationConfiguration


class Workstation:
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

    @property
    def configuration(self) -> WorkstationConfiguration:
        return self._configuration

    @configuration.setter
    def configuration(self, configuration: WorkstationConfiguration) -> None:
        self._configuration = configuration

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
        await self.input_belt.safety_off()
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
