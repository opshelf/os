from dataclasses import dataclass
from enum import IntEnum

from hardware.drivers.utilty_functions import extract_bits, pack_bits


class Color(IntEnum):
    RED = 1
    GREEN = 2
    BLUE = 3


class Direction(IntEnum):
    LEFT = 0
    RIGHT = 1


@dataclass
class MotionSettings:
    value: int | None = None
    enabled: bool = False
    direction: Direction = Direction.LEFT
    speed: int = 0

    def __post_init__(self):
        if self.value is None:
            self.value = pack_bits(
                [
                    (int(self.enabled), 0, 1),
                    (int(self.direction), 1, 1),
                    (self.speed, 2, 8),
                ]
            )
        else:
            self.enabled = bool(extract_bits(self.value, 0, 1))
            self.direction = Direction(extract_bits(self.value, 1, 1))
            self.filter = int(extract_bits(self.value, 2, 8))
