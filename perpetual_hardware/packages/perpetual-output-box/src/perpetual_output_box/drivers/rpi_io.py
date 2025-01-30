from asyncio import Lock
from perpetual_output_box.drivers.base import AbstractIO, DoorState, LockState, BreakBeamState
import gpiod  # type: ignore
from gpiod.line import Direction, Value  # type: ignore


class RpiIO(AbstractIO):
    def __init__(
        self,
        chip_name: str = "/dev/gpiochip0",
        door_pin: int = 5,
        banner_pin: int = 17,
        lock_pin: int = 22,
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
                self._door_pin: gpiod.LineSettings(direction=Direction.INPUT),
                self._banner_pin: gpiod.LineSettings(direction=Direction.INPUT),
            },
        )

    async def get_door_sensor(self) -> DoorState:
        value = self._request.get_value(self._door_pin)
        if value == Value.ACTIVE:
            return DoorState.CLOSED
        return DoorState.OPEN

    async def get_break_beam_sensor(self) -> BreakBeamState:
        value = self._request.get_value(self._banner_pin)
        if value == Value.ACTIVE:
            return BreakBeamState.UNOBSTRUCTED
        return BreakBeamState.OBSTRUCTED

    async def get_lock(self) -> LockState:
        value = self._request.get_value(self._lock_pin)
        if value == Value.ACTIVE:
            return LockState.LOCKED
        return LockState.UNLOCKED

    async def set_door_locked(self, door_locked: LockState) -> None:
        self._request.set_value(
            self._lock_pin, Value.ACTIVE if door_locked == LockState.LOCKED else Value.INACTIVE
        )

    def close(self) -> None:
        self._request.close()
