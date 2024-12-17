from enum import IntEnum

class UserVariables(IntEnum):
    HOMED = 0x00
    HARD_STOPPED = 0x01


class HomedStatus(IntEnum):
    NOT_HOMED = 0x00
    HOMED = 0x01


class HardStopStatus(IntEnum):
    NOT_HARD_STOPPED = 0x00
    HARD_STOPPED = 0x01

