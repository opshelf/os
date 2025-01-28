from perpetual_output_box.io import AbstractIO
import gpiod #type: ignore
from gpiod.line import Direction, Value #type: ignore


class RpiIO(AbstractIO):

    def __init__(self, 
        chip_name: str ='/dev/gpiochip0', 
        door_pin: int = 5, 
        banner_pin: int = 17, 
        lock_pin: int = 22
        ) -> None:
        self._chip_name = chip_name
        self._door_pin = door_pin
        self._banner_pin = banner_pin
        self._lock_pin = lock_pin
        self._request = gpiod.request_lines(
            self._chip_name,
            consumer="output_box",
            config={
                self._lock_pin: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.INACTIVE
                ),
                self._door_pin: gpiod.LineSettings(
                    direction=Direction.INPUT
                ),
                self._banner_pin: gpiod.LineSettings(
                    direction=Direction.INPUT
                ),
            }
        )

    def get_door_sensor(self) -> bool: 
        value = self._request.get_value(self._door_pin)
        if value == Value.ACTIVE:
            return True
        return False

    def get_break_beam_sensor(self) -> bool: 
        value = self._request.get_value(self._banner_pin)
        if value == Value.ACTIVE:
            return True
        return False

    def get_lock(self) -> bool: 
        value = self._request.get_value(self._lock_pin)
        if value == Value.ACTIVE:
            return True
        return False

    def set_door_locked(self, door_locked: bool) -> None:
        self._request.set_value(self._lock_pin, Value.ACTIVE if door_locked else Value.INACTIVE)

    def close(self):
        self._request.close()
