from can import BusABC
from perpetual_p0c.common import CanAxisConfiguration
from perpetual_trinamic.trinamic_axis import TrinamicAxis
from pydantic import BaseModel
import atexit


class OutputBoxOffsets(BaseModel):
    door_open_position: float


class OutputBoxConfiguration(BaseModel):
    door: CanAxisConfiguration
    offsets: OutputBoxOffsets


class OutputBox:
    def __init__(self, bus: BusABC, configuration: OutputBoxConfiguration):
        self._configuration = configuration
        self._door = TrinamicAxis(
            bus,
            module_id=configuration.door.can_id,
            motor_id=0,
            configuration=configuration.door.configuration,
        )
        # atexit.register(self.cleanup)

    def cleanup(self) -> None:
        self._door.stop()

    @property
    def configuration(self) -> OutputBoxConfiguration:
        return self._configuration

    @configuration.setter
    def configuration(self, configuration: OutputBoxConfiguration) -> None:
        self._configuration = configuration

    @property
    def offsets(self) -> OutputBoxOffsets:
        return self._configuration.offsets

    @offsets.setter
    def offsets(self, offsets: OutputBoxOffsets) -> None:
        self._configuration.offsets = offsets

    @property
    def door(self) -> TrinamicAxis:
        return self._door

    def initialize(self) -> None:
        self._door.initialize()

    def home(self) -> None:
        self._door.home()

    def open(self) -> None:
        self._door.move_to_position(self._configuration.offsets.door_open_position)
        self._door.wait_for_move()

    def close(self) -> None:
        self._door.move_to_position(0)
        self._door.wait_for_move()
