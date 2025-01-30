from dataclasses import dataclass
from enum import IntEnum
from can import Message
from pydantic import BaseModel, model_validator, Field
from typing import Self, List
import struct


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

    @model_validator(mode="after")
    def set_error_flags(self) -> Self:
        self.stall_guard_error = bool(self.value & 0x01)
        self.deviation_error = bool(self.value & 0x02)
        return self

    def is_fault(self) -> bool:
        return self.stall_guard_error or self.deviation_error


class StatusFlags(BaseModel):
    value: int
    stall_guard2_status: bool = Field(default=False)
    over_temperature: bool = Field(default=False)
    over_temperature_pre_warning: bool = Field(default=False)
    short_to_ground_a: bool = Field(default=False)
    short_to_ground_b: bool = Field(default=False)
    open_load_a: bool = Field(default=False)
    open_load_b: bool = Field(default=False)
    stand_still: bool = Field(default=False)

    @model_validator(mode="after")
    def set_status_flags(self) -> Self:
        self._value = self.value
        self.stall_guard2_status: bool = bool(self.value & 0x01)
        self.over_temperature: bool = bool(self.value & 0x02)
        self.over_temperature_pre_warning: bool = bool(self.value & 0x04)
        self.short_to_ground_a: bool = bool(self.value & 0x08)
        self.short_to_ground_b: bool = bool(self.value & 0x10)
        self.open_load_a: bool = bool(self.value & 0x20)
        self.open_load_b: bool = bool(self.value & 0x40)
        self.stand_still: bool = bool(self.value & 0x80)
        return self

    def is_fault(self) -> bool:
        return (
            self.open_load_b
            or self.open_load_a
            or self.short_to_ground_a
            or self.short_to_ground_b
            or self.over_temperature
            or self.stall_guard2_status
        )


@dataclass
class TmclMessage:
    module_address: int
    command_number: int
    type_number: int
    motor_number: int
    value: int

    def into_can_message(self) -> Message:
        payload = [self.command_number, self.type_number, self.motor_number]
        payload.extend(list(struct.pack(">i", self.value)))
        return Message(
            arbitration_id=int(self.module_address),
            data=payload,
            is_extended_id=False,
        )

    def into_serial_buffer(self) -> List[int]:
        payload = [self.module_address, self.command_number, self.type_number, self.motor_number]
        payload.extend(list(struct.pack(">i", self.value)))
        checksum = 0
        for d in payload:
            checksum += d
        checksum &= 0xFF
        payload.append(checksum)
        return payload


@dataclass
class TmclReply:
    module_address: int
    command_number: int
    status: int
    value: int

    @staticmethod
    def from_serial_buffer(data: List[int] | bytearray | bytes) -> "TmclReply":
        if len(data) != 9:
            raise Exception("Invalid Data Length")
        checksum = TmclReply.calculate_checksum(data[:-1])
        if checksum != data[-1]:
            raise Exception("Invalid Checksum in Reception")
        payload = bytearray(data[4:8])
        return TmclReply(
            module_address=data[1],
            command_number=data[3],
            status=data[2],
            value=struct.unpack(">i", payload)[0],
        )

    @staticmethod
    def calculate_checksum(data: List[int] | bytearray | bytes) -> int:
        checksum = 0
        for d in data:
            checksum += d
        checksum &= 0xFF
        return checksum


class MoveType(IntEnum):
    ABSOLUTE = 0
    RELATIVE = 1
    COORDINATE = 2


# TODO: REFACTOR TO USE SINGLE BASE EXCEPTION FOR INVALID COMMANDS


class TmclCommandLoaded(Exception):
    """Tmcl Command Loaded"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclWrongChecksum(Exception):
    """Tmcl Wrong Checksum"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclInvalidCommand(Exception):
    """Tmcl Invalid Command"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclWrongType(Exception):
    """Tmcl Wrong Type"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclInvalidValue(Exception):
    """Tmcl Invalid Value"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclEepromLocked(Exception):
    """Tmcl Eeprom Locked"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclCommandNotAvailable(Exception):
    """Tmcl Command Not Available"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class UnknownTmclResponse(Exception):
    """Tmcl Unknown Response"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"
