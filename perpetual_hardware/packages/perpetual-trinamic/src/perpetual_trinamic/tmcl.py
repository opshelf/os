from dataclasses import dataclass
from enum import IntEnum
from pydantic import BaseModel, model_validator, Field
from typing import Self


class TMCLCommand:
    ROR = 1
    ROL = 2
    MST = 3
    MVP = 4
    SAP = 5
    GAP = 6
    STAP = 7
    RSAP = 8
    SGP = 9
    GGP = 10
    STGP = 11
    RSGP = 12
    RFS = 13
    SIO = 14
    GIO = 15
    CALC = 19
    COMP = 20
    JC = 21
    JA = 22
    CSUB = 23
    RSUB = 24
    EI = 25
    DI = 26
    WAIT = 27
    STOP = 28
    SAC = 29
    SCO = 30
    GCO = 31
    CCO = 32
    CALCX = 33
    AAP = 34
    AGP = 35
    CLE = 36
    VECT = 37
    RETI = 38
    ACO = 39
    CALCVV = 40
    CALCVA = 41
    CALCAV = 42
    CALCVX = 43
    CALCXV = 44
    CALCV = 45
    MVPA = 46
    RST = 48
    DJNZ = 49
    ROLA = 50
    RORA = 51
    SIV = 55
    GIV = 56
    AIV = 57
    TMCL_UF0 = 64
    TMCL_UF1 = 65
    TMCL_UF2 = 66
    TMCL_UF3 = 67
    TMCL_UF4 = 68
    TMCL_UF5 = 69
    TMCL_UF6 = 70
    TMCL_UF7 = 71
    CALL = 80

    STOP_APPLICATION = 128
    RUN_APPLICATION = 129
    STEP_APPLICATION = 130
    RESET_APPLICATION = 131
    START_DOWNLOAD_MODE = 132
    QUIT_DOWNLOAD_MODE = 133
    READ_TMCL_MEMORY = 134
    GET_APPLICATION_STATUS = 135
    GET_FIRMWARE_VERSION = 136
    RESTORE_FACTORY_SETTINGS = 137
    TARGET_POSITION_REACHED = 138
    RAMDEBUG = 142
    ASSIGNMENT = 143
    WRITE_MC = 146
    WRITE_DRV = 147
    READ_MC = 148
    READ_DRV = 149
    GET_INFO = 157

    BOOT_ERASE_ALL = 200
    BOOT_WRITE_BUFFER = 201
    BOOT_WRITE_PAGE = 202
    BOOT_GET_CHECKSUM = 203
    BOOT_READ_MEMORY = 204
    BOOT_START_APPL = 205
    BOOT_GET_INFO = 206
    BOOT_WRITE_LENGTH = 208
    BOOT = 242


class TMCLStatus:
    SUCCESS = 100
    COMMAND_LOADED = 101
    WRONG_CHECKSUM = 1
    INVALID_COMMAND = 2
    WRONG_TYPE = 3
    INVALID_VALUE = 4
    EEPROM_LOCKED = 5
    COMMAND_NOT_AVAILABLE = 6

class TrinamicErrorFlags(BaseModel):
    value: int
    stall_guard_error: bool = Field(default=False)
    deviation_error: bool = Field(default=False)


    @model_validator(mode='after')
    def set_error_flags(self) -> Self:
        self.stall_guard_error = bool(self.value & 0x01)
        self.deviation_error = bool(self.value & 0x02)
        return self


    def is_fault(self) -> bool:
        return self.stall_guard_error or self.deviation_error

    """
    def __repr__(self) -> str:
        return f"ErrorFlags(stall_guard={self.stall_guard_error},\
deviation_error={self.deviation_error})"
    """


class StatusFlags(BaseModel):
    stall_guard2_status: bool
    over_temperature: bool
    over_temperature_pre_warning: bool
    short_to_ground_a: bool
    short_to_ground_b: bool
    open_load_a: bool
    open_load_b: bool
    stand_still: bool

    def __init__(self, value: int) -> None:
        self._value = value
        self.stall_guard2_status: bool = bool(value & 0x01)
        self.over_temperature: bool = bool(value & 0x02)
        self.over_temperature_pre_warning: bool = bool(value & 0x04)
        self.short_to_ground_a: bool = bool(value & 0x08)
        self.short_to_ground_b: bool = bool(value & 0x10)
        self.open_load_a: bool = bool(value & 0x20)
        self.open_load_b: bool = bool(value & 0x40)
        self.stand_still: bool = bool(value & 0x80)

    """
    @property
    def stall_guard2_status(self) -> bool:
        return self._stall_guard2_status

    @property
    def over_temperature(self) -> bool:
        return self._over_temperature

    @property
    def over_temperature_pre_warning(self) -> bool:
        return self._over_temperature_pre_warning

    @property
    def short_to_ground_a(self) -> bool:
        return self._short_to_ground_a

    @property
    def short_to_ground_b(self) -> bool:
        return self._short_to_ground_b

    @property
    def open_load_a(self) -> bool:
        return self._open_load_a

    @property
    def open_load_b(self) -> bool:
        return self._open_load_b

    @property
    def stand_still(self) -> bool:
        return self._stand_still
    """

    def is_fault(self) -> bool:
        return (
            self.open_load_b
            or self.open_load_a
            or self.short_to_ground_a
            or self.short_to_ground_b
            or self.over_temperature
            or self.stall_guard2_status
        )

    """
    def __repr__(self) -> str:
        return f"StatusFlags(\
stall_guard2_status={self.stall_guard2_status},\
overtemperature={self.over_temperature},\
overtempertaure_pre_warning={self.over_temperature_pre_warning},\
short_to_ground_a={self.short_to_ground_a},\
short_to_ground_b={self.short_to_ground_b},\
open_load_a={self.open_load_a},\
open_load_b={self.open_load_b},\
stand_still={self.stand_still},\
)"

    """


@dataclass
class TmclMessage:
    module_address: int
    command_number: int
    type_number: int
    motor_number: int
    value: int


@dataclass
class TmclReply:
    module_address: int
    command_number: int
    status: int
    value: int


class MoveType(IntEnum):
    ABSOLUTE = 0
    RELATIVE = 1
    COORDINATE = 2
