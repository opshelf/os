from dataclasses import dataclass
from enum import IntEnum

from hardware.drivers.utilty_functions import extract_bits, pack_bits


# --- Control ("control") ---
class GripperMode(IntEnum):
    PARAMETER_CONTROL = 0x0
    PRESET_CONTROL = 0x1


class GripperCommand(IntEnum):
    STOP = 0x0
    MOVE_TO_TARGET_POSITION = 0x1


class SelfCheckDirection(IntEnum):
    CLOSE = 0x0
    OPEN = 0x1


class ControlFunction(IntEnum):
    RESERVE = 0x0
    HALF_FORCE_HALF_SPEED_OPEN = 0x1
    HALF_FORCE_HALF_SPEED_CLOSE = 0x2
    FULL_FORCE_FULL_SPEED_OPEN = 0x3
    FULL_FORCE_FULL_SPEED_CLOSE = 0x4
    LOW_FORCE_LOW_SPEED_OPEN = 0x5
    LOW_FORCE_LOW_SPEED_CLOSE = 0x6
    PRESET_1 = 0x8
    PRESET_2 = 0x9
    PRESET_3 = 0xA
    PRESET_4 = 0xB
    PRESET_5 = 0xC
    PRESET_6 = 0xD
    PRESET_7 = 0xE
    PRESET_8 = 0xF


@dataclass
class ControlRegister:
    """Represents the main function register of the electric gripper, including
    functions such as electric gripper enabling, mode switching and motion control, etc"""

    value: int | None = None
    enable_gripper: bool = True
    mode: GripperMode = GripperMode.PRESET_CONTROL
    command: GripperCommand = GripperCommand.STOP
    enable_self_check: bool = False
    self_check_direction: SelfCheckDirection = (
        SelfCheckDirection.CLOSE
    )
    function: ControlFunction = ControlFunction.RESERVE

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.enable_gripper), 0, 1),
                (int(self.mode), 1, 1),
                (int(self.command), 3, 1),
                (int(self.enable_self_check), 4, 1),
                (int(self.self_check_direction), 5, 1),
                (int(self.function), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.enable_gripper = bool(extract_bits(self.value, 0, 1))
            self.mode = bool(extract_bits(self.value, 1, 1))
            self.command = bool(extract_bits(self.value, 3, 1))
            self.enable_self_check = bool(
                extract_bits(self.value, 4, 1)
            )
            self.self_check_direction = bool(
                extract_bits(self.value, 5, 1)
            )
            self.function = ControlFunction(
                extract_bits(self.value, 8, 8)
            )


# --- Position setting ("position setting") ---
@dataclass
class PositionSetting:
    value: int | None = None
    position: int = 0  # 0x00: fully opened, 0xFF: fully closed

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.position), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.position = extract_bits(self.value, 8, 8)


# --- Speed and force setting ("speed and force setting") ---
@dataclass
class SpeedForceSetting:
    value: int | None = None
    speed: int = 0  # 0x00: min speed, 0xFF: max speed
    force: int = 0  # 0x00: min force, 0xFF: max force

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.speed), 0, 8),
                (int(self.force), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.speed = extract_bits(self.value, 0, 8)
            self.force = extract_bits(self.value, 8, 8)


# --- Position and speed preset ("position and speed preset N") ---
@dataclass
class PositionSpeedPreset:
    value: int | None = None
    position: int = (
        0  # target position (0x00: fully opened, 0xFF: fully closed)
    )
    speed: int = (
        0  # target speed (0x00: lowest speed, 0xFF: highest speed)
    )

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.position), 0, 8),
                (int(self.speed), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.position = extract_bits(self.value, 0, 8)
            self.speed = extract_bits(self.value, 8, 8)


# --- Gripper status ("gripper status") ---
class StatusCode(IntEnum):
    RESET_OR_INSPECTION = 0x0
    ACTIVATING = 0x1
    INVALID = 0x2
    ACTIVATED = 0x3


class FingerStatus(IntEnum):
    MOVING_TO_POSITION = 0x0
    CONTACT_WITH_FINGER_OPENED = 0x1
    CONTACT_WITH_FINGER_CLOSED = 0x2
    IN_POSITION_NO_CONTACT = 0x3


@dataclass
class GripperStatus:
    """Represents the main function register of the electric gripper, including
    functions such as electric gripper enabling, mode switching and motion control, etc"""

    value: int | None = None
    is_enabled: bool = False
    is_workpiece_dropped: bool = False
    mode: GripperMode = GripperMode.PARAMETER_CONTROL
    command: GripperCommand = GripperCommand.STOP
    status: StatusCode = StatusCode.RESET_OR_INSPECTION
    finger_status: FingerStatus = FingerStatus.MOVING_TO_POSITION

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.is_enabled), 0, 1),
                (int(self.is_workpiece_dropped), 1, 1),
                (int(self.mode), 2, 1),
                (int(self.command)),
                (int(self.status), 4, 2),
                (int(self.finger_status), 6, 2),
            ]
            self.value = pack_bits(bits)
        else:
            self.is_enabled = bool(extract_bits(self.value, 0, 1))
            self.is_workpiece_dropped = bool(
                extract_bits(self.value, 1, 1)
            )
            self.mode = GripperMode(extract_bits(self.value, 2, 1))
            self.command = GripperCommand(
                extract_bits(self.value, 3, 1)
            )
            self.status = StatusCode(extract_bits(self.value, 4, 2))
            self.finger_status = FingerStatus(
                extract_bits(self.value, 6, 2)
            )


# --- Fault and position status ("fault and position status") ---
@dataclass
class FaultPositionStatus:
    value: int | None = None
    activation_fault: bool = False
    command_fault: bool = False
    communication_fault: bool = False
    overcurrent_fault: bool = False
    voltage_fault: bool = False
    enabling_fault: bool = False
    overtemperature_fault: bool = False
    self_fault: bool = False
    current_position: int = 0

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.activation_fault), 0, 1),
                (int(self.command_fault), 1, 1),
                (int(self.communication_fault), 2, 1),
                (int(self.overcurrent_fault), 3, 1),
                (int(self.voltage_fault), 4, 1),
                (int(self.enabling_fault), 5, 1),
                (int(self.overtemperature_fault), 6, 1),
                (int(self.self_fault), 7, 1),
                (int(self.current_position), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.activation_fault = bool(
                extract_bits(self.value, 0, 1)
            )
            self.command_fault = bool(extract_bits(self.value, 1, 1))
            self.communication_fault = bool(
                extract_bits(self.value, 2, 1)
            )
            self.overcurrent_fault = bool(
                extract_bits(self.value, 3, 1)
            )
            self.voltage_fault = bool(extract_bits(self.value, 4, 1))
            self.enabling_fault = bool(extract_bits(self.value, 5, 1))
            self.overtemperature_fault = bool(
                extract_bits(self.value, 6, 1)
            )
            self.self_fault = bool(extract_bits(self.value, 7, 1))
            self.current_position = extract_bits(self.value, 8, 8)


# --- Speed and force status ("speed and force status") ---
@dataclass
class SpeedForceStatus:
    value: int | None = None
    speed: int = 0
    force: int = 0

    def __post_init__(self):
        if self.value is None:
            bits = [(int(self.speed), 0, 8), (int(self.force), 8, 8)]
            self.value = pack_bits(bits)
        else:
            self.speed = extract_bits(self.value, 0, 8)
            self.force = extract_bits(self.value, 8, 8)


# --- Voltage and temperature status ("voltage and temperature status") ---
@dataclass
class VoltageTemperatureStatus:
    value: int | None = None
    voltage: int = 0
    temperature: int = 0

    def __post_init__(self):
        if self.value is None:
            bits = [
                (int(self.voltage), 0, 8),
                (int(self.temperature), 8, 8),
            ]
            self.value = pack_bits(bits)
        else:
            self.voltage = extract_bits(self.value, 0, 8)
            self.temperature = extract_bits(self.value, 8, 8)
