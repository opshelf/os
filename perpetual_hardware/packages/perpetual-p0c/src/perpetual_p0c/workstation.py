from can import BusABC
from perpetual_p0c.common import CanAxisConfiguration
from perpetual_trinamic.trinamic_axis import TrinamicAxis
from pydantic import BaseModel
from perpetual_p0c.output_box import OutputBoxConfiguration, OutputBox
from perpetual_p0c.common import CanAxisConfiguration
import atexit


class WorkstationConfiguration(BaseModel):
    outputbox_configuration: OutputBoxConfiguration
    input_belt_configuration: CanAxisConfiguration
    output_belt_configuration: CanAxisConfiguration
    input_belt_speed: int
    output_belt_speed: int


class Workstation:
    def __init__(self, bus: BusABC, configuration: WorkstationConfiguration):
        self._configuration = configuration
        self._output_box = OutputBox(bus, self._configuration.outputbox_configuration)
        self._input_belt = TrinamicAxis(
            bus,
            module_id=self._configuration.input_belt_configuration.can_id,
            motor_id=0,
            configuration=self._configuration.input_belt_configuration.configuration,
        )
        self._output_belt = TrinamicAxis(
            bus,
            module_id=self._configuration.output_belt_configuration.can_id,
            motor_id=0,
            configuration=self._configuration.output_belt_configuration.configuration,
        )
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        self._output_belt.stop()
        self._input_belt.stop()

    def home(self) -> None:
        self.home_output()
        self.input_belt.initialize()
        self.output_belt.initialize()

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

    def start_input_belt(self) -> None:
        self.input_belt.initialize()
        self.input_belt.rotate(self._configuration.input_belt_speed)

    def stop_input_belt(self) -> None:
        self.input_belt.stop()

    def start_output_belt(self) -> None:
        self.output_belt.initialize()
        self.output_belt.rotate(self._configuration.output_belt_speed)

    def stop_output_belt(self) -> None:
        self.output_belt.stop()

    def open_output(self) -> None:
        self.output_box.open()

    def close_output(self) -> None:
        self.output_box.close()

    def home_output(self) -> None:
        self.output_box.home()
