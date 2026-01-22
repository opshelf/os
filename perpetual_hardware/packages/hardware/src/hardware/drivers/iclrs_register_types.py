from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar


# --- Control mode ---
class ControlMode(IntEnum):
    OPEN_LOOP = 0x00
    RESERVED = 0x01
    CLOSED_LOOP = 0x02


# --- Motor direction ---
class MotorDirection(IntEnum):
    CW = 0x00
    CCW = 0x01


# --- Digital I/O settings ---
class DigitalInputFunction(IntEnum):
    """Represents digital input function setting"""

    INVALID = 0x00
    ENABLE = 0x08
    COMMAND_TRIGGER = 0x020
    HOMING_TRIGGER = 0x21
    QUICK_STOP = 0x22
    JOG_PLUS = 0x23
    JOG_MINUS = 0x24
    POSITIVE_LIMIT = 0x25
    NEGATIVE_LIMIT = 0x26
    HOME_SWITCH = 0x27
    PATH_ADDRESS_0 = 0x28
    PATH_ADDRESS_1 = 0x29
    PATH_ADDRESS_2 = 0x2A
    PATH_ADDRESS_3 = 0x2B
    JOG_2 = 0x2C


class DigitalInputFilter(IntEnum):
    """Represents digital input time filter [ms] setting"""

    FILTER_10_MS = 0b0000
    FILTER_1_MS = 0b0001
    FILTER_2_MS = 0b0010
    FILTER_3_MS = 0b0011
    FILTER_4_MS = 0b0100
    FILTER_5_MS = 0b0101
    FILTER_6_MS = 0b0110
    FILTER_8_MS = 0b0111
    FILTER_15_MS = 0b1000
    FILTER_20_MS = 0b1001
    FILTER_30_MS = 0b1010
    FILTER_40_MS = 0b1011
    FILTER_50_MS = 0b1100
    FILTER_100_MS = 0b1101
    FILTER_200_MS = 0b1110
    FILTER_500_MS = 0b1111


class DigitalOutputFunction(IntEnum):
    """Represents digital output function setting"""

    INVALID = 0x00
    COMMAND_COMPLETE = 0x20
    PATH_COMPLETE = 0x21
    HOMING_COMPLETE = 0x22
    ALARM = 0x25
    BRAKE = 0x24
    IN_POSITION = 0x23


class NormallyOpenedOrClosed(IntEnum):
    """Represents digital input and output mode setting"""

    NORMALLY_OPENED = 0
    NORMALLY_CLOSED = 1


@dataclass
class DigitalInputSettings:
    """Represents digital input settings for iCL-RS series motor"""

    function: DigitalInputFunction = DigitalInputFunction.INVALID
    mode: NormallyOpenedOrClosed = (
        NormallyOpenedOrClosed.NORMALLY_OPENED
    )
    filter: DigitalInputFilter = DigitalInputFilter.FILTER_10_MS
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            self.value = (
                self.filter << 8 | self.mode << 7 | self.function
            )
        else:
            self.function = DigitalInputFunction(extract_bits(self.value, 0, 7))
            self.mode = NormallyOpenedOrClosed(extract_bits(self.value, 7, 1))
            self.filter = DigitalInputFilter(extract_bits(self.value, 8, 4))

    def __str__(self):
        filter_str = f"filter = {self.filter.name.lower().replace('filter', '').replace('_', '')}"
        mode_str = (
            f"mode = {self.mode.name.lower().replace('_', ' ')}"
        )
        function_str = f"function = {self.function.name.lower().replace('_', ' ')}"
        return f"settings({function_str}, {mode_str}, {filter_str})"


@dataclass
class DigitalOutputSettings:
    """Represents digital output settings for iCL-RS series motor"""

    function: DigitalOutputFunction = DigitalOutputFunction.INVALID
    mode: NormallyOpenedOrClosed = (
        NormallyOpenedOrClosed.NORMALLY_OPENED
    )
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            # value was not given, create it with the settings fields
            self.value = self.mode << 7 | self.function
        else:
            # value was given, parse value to get settings fields
            self.function = DigitalOutputFunction(
                self.value & 0b111111
            )
            self.mode = NormallyOpenedOrClosed(self.value >> 7 & 0b1)

    def __str__(self):
        mode_str = (
            f"mode = {self.mode.name.lower().replace('_', ' ')}"
        )
        function_str = f"function = {self.function.name.lower().replace('_', ' ')}"
        return f"settings({function_str}, {mode_str})"


# --- Digital I/O states ---


@dataclass
class DigitalInputStates:
    value: int | None = None
    si_1: int = 0
    si_2: int = 0
    si_3: int = 0
    si_4: int = 0
    si_5: int = 0
    si_6: int = 0
    si_7: int = 0

    def __post_init__(self):
        if self.value == None:
            self.value = (
                self.si_7 << 6
                | self.si_6 << 5
                | self.si_5 << 4
                | self.si_4 << 3
                | self.si_3 << 2
                | self.si_2 << 1
                | self.si_1
            )
        else:
            self.si_7 = self.value >> 6 & 1
            self.si_6 = self.value >> 5 & 1
            self.si_5 = self.value >> 4 & 1
            self.si_4 = self.value >> 3 & 1
            self.si_3 = self.value >> 2 & 1
            self.si_2 = self.value >> 1 & 1
            self.si_1 = self.value & 1

    def __str__(self):
        return (
            f" si1 to si7 : ({self.si_1}, {self.si_2}, {self.si_3}, {self.si_4}, "
            f"{self.si_5}, {self.si_6}, {self.si_7})"
        )


@dataclass
class DigitalOutputStates:
    so_1: int = 0
    so_2: int = 0
    so_3: int = 0
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            self.value = self.so_3 << 2 | self.so_2 << 1 | self.so_1
        else:
            self.so_3 = self.value >> 2 & 1
            self.so_2 = self.value >> 1 & 1
            self.so_1 = self.value & 1

    def __str__(self):
        return (
            f" so1 to so3 : ({self.so_1}, {self.so_2}, {self.so_3})"
        )


# --- Motion state ---
@dataclass
class MotionStatus:
    value: int | None = None
    is_fault: bool = False
    is_enabled: bool = False
    is_running: bool = False
    command_completed: bool = False
    path_completed: bool = False
    homing_completed: bool = False

    def __post_init__(self):
        if self.value is None:
            self.value = (
                self.homing_completed << 5
                | self.path_completed << 4
                | self.command_completed << 3
                | self.is_running << 2
                | self.is_enabled << 1
                | self.is_fault
            )
        else:
            self.is_fault = bool(self.value >> 0 & 1)
            self.is_enabled = bool(self.value >> 1 & 1)
            self.is_running = bool(self.value >> 2 & 1)
            self.command_completed = bool(self.value >> 4 & 1)
            self.path_completed = bool(self.value >> 5 & 1)
            self.homing_completed = bool(self.value >> 6 & 1)

    def __str__(self):
        return (
            f"Fault: {self.is_fault}, Enabled: {self.is_enabled}, "
            f"Running: {self.is_running}, Command completed: {self.command_completed}, "
            f"Path completed: {self.path_completed}, "
            f"Homing completed: {self.homing_completed}"
        )


# --- Control word and status response
class ControlWordFunction(IntEnum):
    INVALID = 0x0000
    RESET_CURRENT_ALARM = 0x1111
    RESET_HISTORY_ALARM = 0x1122
    SAVE_ALL_PARAMETERS_TO_EEPROM = 0x2211
    RESET_PARAMETERS_EXCLUDE_MOTOR_PARAMETERS = 0x2222
    RESET_ALL_PARAMETERS = 0x2233
    SAVE_ALL_MAPPINGS_INTO_EEPROM = 0x2244
    JOG_CW = 0x4001
    JOG_CCW = 0x4002


class ControlWordStatus(IntEnum):
    INVALID = 0x1111
    OK = 0x5555
    FAULT = 0xAAAA


# --- Alarm ---
@dataclass
class AlarmDetectionSelection:
    enable_over_current: bool
    enable_over_voltage: bool
    enable_adc_sampling_failure: bool
    enable_locked_shaft_alarm: bool
    enable_eeprom_alarm: bool
    enable_autotuning_alarm: bool
    value: int = field(init=False)

    def __post_init__(self):
        self.value = (
            self.enable_autotuning_alarm << 6
            | self.enable_eeprom_alarm << 5
            | self.enable_locked_shaft_alarm << 4
            | self.enable_adc_sampling_failure << 3
            | self.enable_over_voltage << 1
            | self.enable_over_current << 0
        )


class AlarmCode(IntEnum):
    NO_ALARM = 0x00
    OVER_CURRENT = 0x01
    OVER_VOLTAGE = 0x02
    STALL_TORQUE = 0x020
    ADC_SAMPLING_ERROR = 0x40
    SHAFT_LOCKING_ERROR = 0x80
    AUTO_TUNING_ERROR = 0x100
    EEPROM_ERROR = 0x200


@dataclass
class Alarm:
    code: AlarmCode = field(init=False)
    value: int
    ALARM_MAP: ClassVar[dict] = {
        AlarmCode.NO_ALARM: ("No alarm."),
        AlarmCode.OVER_CURRENT: (
            "Restart the controller. "
            "If error persists, check if the motor is short-circuited or "
            "disconnected from the controller."
        ),
        AlarmCode.OVER_VOLTAGE: (
            "Restart the controller. "
            "If error persists, check the voltage of the power supply."
        ),
        AlarmCode.STALL_TORQUE: ("Intentional during homing."),
        AlarmCode.ADC_SAMPLING_ERROR: (
            "Restart the controller. "
            "If error persists, there is a hardware failure."
        ),
        AlarmCode.SHAFT_LOCKING_ERROR: (
            "Check if the motor wire is broken."
        ),
        AlarmCode.EEPROM_ERROR: (
            "Connect controller to the Leadshine software and "
            "reset to factory parameters. If error persists, there is a hardware "
            "failure."
        ),
        AlarmCode.AUTO_TUNING_ERROR: (
            "Restart the controller. If error persists, "
            "disable auto-tuning by register 0x01AB."
        ),
    }

    def __post_init__(self):
        self.code = AlarmCode(self.value)

    def __str__(self):
        alarm_name = self.code.name.lower().replace("_", " ")
        text = f"{alarm_name}. To troubleshoot: {self.ALARM_MAP[self.code]}"
        return text


# --- Path control ("pr control")---
class PathControlCommand(IntEnum):
    STATUS_DONE_AND_READY = 0x0000
    STATUS_RUNNING = 0x0100
    PATH_MOTION = 0x10
    HOMING = 0x20
    ZERO_CURRENT_POSITION = 0x21
    QUICK_STOP = 0x40
    COMMAND_COMPLETE_WAIT_FOR_POSITIONING = 0x200


class PathControlStatus(IntEnum):
    NONE = 0
    PATH_RUNNING = 1
    PATH_COMPLETE = 2
    COMMAND_COMPLETED = 3


@dataclass
class PathControl:
    command: PathControlCommand = PathControlCommand.PATH_MOTION
    status: PathControlStatus = PathControlStatus.NONE
    path: int = 0
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            if self.command == PathControlCommand.PATH_MOTION:
                self.value = (
                    PathControlCommand.PATH_MOTION + self.path
                )
            else:
                self.value = self.command
        else:
            # Commands (written to register)
            if self.value & 0xF0 == PathControlCommand.PATH_MOTION:
                self.command = PathControlCommand.PATH_MOTION
                self.path = self.value & 0x0F
                self.status = PathControlStatus.NONE
            elif self.value:
                self.command = PathControlCommand(self.value)
                self.status = PathControlStatus.NONE
            # Status (read from register)
            if self.value & 0xFF0 == 0x100:
                self.status = PathControlStatus.PATH_RUNNING
                self.path = self.value & 0xF
            elif self.value == 0:
                self.status = PathControlStatus.PATH_COMPLETE
            elif self.value == 0x200:
                self.status = PathControlStatus.COMMAND_COMPLETED


# --- Homing ---
class HomingDirection(IntEnum):
    CCW = 0
    CW = 1


class HomingType(IntEnum):
    DETECTED_BY_LIMIT_SWITCH = 0
    DETECTED_BY_HOME_SWITCH = 1


@dataclass
class HomingMode:
    direction: HomingDirection = HomingDirection.CCW
    move_to_specified_point_after_homing: bool = True
    mode: HomingType = HomingType.DETECTED_BY_LIMIT_SWITCH
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            self.value = (
                self.mode << 2
                | self.move_to_specified_point_after_homing << 1
                | self.direction
            )
        else:
            self.mode = HomingType(self.value >> 2 & 0b1)
            self.move_to_specified_point_after_homing = bool(
                self.value >> 1 & 0b1
            )
            self.direction = HomingDirection(self.value & 0b1)


@dataclass
class HomeSwitchPosition:
    value: int
    high_bits: int = field(init=False)
    low_bits: int = field(init=False)

    def __post_init__(self):
        self.high_bits = self.value >> 16 & 0xFFFF
        self.low_bits = self.value & 0xFFFF


@dataclass
class HomingStopPosition:
    value: int
    high_bits: int = field(init=False)
    low_bits: int = field(init=False)

    def __post_init__(self):
        self.high_bits = self.value >> 16 & 0xFFFF
        self.low_bits = self.value & 0xFFFF


# --- Path settings ---
class PathOperationMode(IntEnum):
    NO_ACTION = 0
    POSITION_MODE = 1
    VELOCITY_MODE = 2
    HOMING_MODE = 3


class PathCoordinate(IntEnum):
    ABSOLUTE = 0
    RELATIVE = 1


@dataclass
class PathSettings:
    value: int | None = None
    mode: PathOperationMode = PathOperationMode.NO_ACTION
    enable_interrupt: bool = False
    enable_overlap: bool = False
    coordinate: PathCoordinate = PathCoordinate.ABSOLUTE
    jump_to: int = 0
    enable_jump: bool = False

    def __post_init__(self):
        if self.value is None:
            self.value = (
                self.enable_jump << 14
                | self.jump_to << 8
                | self.coordinate << 6
                | int(self.enable_overlap) << 5
                | int(self.enable_interrupt) << 4
                | self.mode
            )
        else:
            self.enable_jump = bool(self.value >> 14 & 0b1)
            self.jump_to = self.value >> 8 & 0b1111
            self.coordinate = PathCoordinate(self.value >> 6 & 0b1)
            self.enable_overlap = bool(self.value >> 5 & 0b1)
            self.enable_interrupt = bool(self.value >> 4 & 0b1)
            self.mode = PathOperationMode(self.value & 0b1111)

    def __str__(self):
        mode_text = self.mode.name.lower().replace("_", " ")
        coordinate_text = self.coordinate.name.lower().replace(
            "_", " "
        )
        return (
            f"(mode = {mode_text}, enable interrupt = {self.enable_interrupt}, "
            f"enable overlap = {self.enable_overlap}, coordinate = {coordinate_text}, "
            f"jump to path = {self.jump_to}, enable jump = {self.enable_jump}"
        )


# -- Path global control ("pr global control" ---


class CtrgEdge(IntEnum):
    RISING_EDGE = 0
    DOUBLE_EDGE = 1


class CtrgType(IntEnum):
    USE_CTRG_EDGE = 0
    LEVEL = 1


@dataclass
class PathGlobalControl:
    ctrg_edge: CtrgEdge = CtrgEdge.RISING_EDGE
    enable_soft_limit: bool = False
    enable_autohoming_after_powerup: bool = False
    ctrg_trigger_type: CtrgType = CtrgType.USE_CTRG_EDGE
    value: int | None = None

    def __post_init__(self):
        if self.value is None:
            self.value = (
                self.ctrg_trigger_type << 4
                | self.enable_autohoming_after_powerup << 2
                | self.enable_soft_limit << 1
                | self.ctrg_edge
            )
        else:
            self.ctrg_edge = CtrgEdge(self.value & 1)
            self.enable_soft_limit = bool(self.value >> 1 & 1)
            self.enable_autohoming_after_powerup = bool(
                self.value >> 1 & 1
            )
            self.ctrg_trigger_type = CtrgType(self.value >> 4 & 1)


@dataclass
class BaudRate:
    value: int | None = None
    baud_rate: str = None

    def __post_init__(self):
        mapping = {
            "2400": 0,
            "4800": 1,
            "9600": 2,
            "19200": 3,
            "38400": 4,
            "57600": 5,
            "115200": 6,
        }
        if self.value is None:
            try:
                self.value = mapping[self.baud_rate]
            except KeyError:
                raise ValueError("Invalid baud rate")
        else:
            try:
                self.baud_rate = next(
                    (k for k, v in mapping.items() if v == self.value)
                )
            except StopIteration:
                raise ValueError("Invalid baud rate")

    def __str__(self):
        return f"{self.baud_rate}"


# --- Utility functions ---
def extract_bits(value: int, start_bit: int, num_bits: int) -> int:
    """Extracts bits from an integer value.

    Args:
        value (int): The integer value to extract bits from.
        start_bit (int): The starting bit position (0-indexed).
        num_bits (int): The number of bits to extract.

    Returns:
        int: The extracted bits as an integer.
    """
    mask = (1 << num_bits) - 1
    return (value >> start_bit) & mask