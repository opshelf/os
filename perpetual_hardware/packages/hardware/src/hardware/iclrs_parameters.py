'''
Tests in ./tests/test_iclrs_parameters.py
'''
from dataclasses import dataclass, field, fields
from enum import IntEnum
import csv
from typing import Union

class ModbusErrorCode(IntEnum):
  INVALID_FUNCTION_CODE = 0x01
  INVALID_ADDRESS = 0x02
  INVALID_DATA = 0x03
  INVALID_CRC = 0x08

# Control mode
class ControlMode(IntEnum):
  OPEN_LOOP = 0x00
  RESERVED = 0x01
  CLOSED_LOOP = 0x02

# Motor direction
class MotorDirection(IntEnum):
  CW = 0x00
  CCW = 0x01

# Digital I/O
class DigitalInputFunction(IntEnum):
  '''Represents digital input function setting
  '''
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
  '''Represents digital input time filter [ms] setting
  '''
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
  '''Represents digital output function setting
  '''
  COMMAND_COMPLETE = 0x20
  PATH_COMPLETE = 0x21
  HOMING_COMPLETE = 0x22
  ALARM = 0x25
  BRAKE = 0x24
  IN_POSITION = 0x23

class DigitalInputOutputMode(IntEnum):
  '''Represents digital input and output mode setting
  '''
  NORMALLY_OPENED = 0
  NORMALLY_CLOSED = 1

@dataclass
class DigitalInputSettings():
  '''Represents digital input settings for iCL-RS series motor
  '''
  function: DigitalInputFunction = None
  mode: DigitalInputOutputMode = None
  filter: DigitalInputFilter = None
  value: int = None
  def __post_init__(self):
    if self.value == None:
      # value was not given, create it with the settings fields
      self.value = self.filter << 8 | self.mode << 7  | self.function
    else:
      # value was given, parse value to get settings fields
      self.function = DigitalInputFunction(self.value & 0b111111)
      self.mode = DigitalInputOutputMode(self.value >> 7 & 0b1)
      self.filter = DigitalInputFilter(self.value >> 8 & 0b1111)
  def __str__(self):
    return f'{self.filter.name} | {self.mode.name} | {self.function.name} -> {self.value}'

@dataclass
class DigitalOutputSettings():
  '''Represents digital output settings for iCL-RS series motor
  '''
  function: DigitalOutputFunction = None
  mode: DigitalInputOutputMode = None
  value: int = None
  def __post_init__(self):
    if self.value == None:
      # value was not given, create it with the settings fields
      self.value = self.mode << 7  | self.function
    else:
      # value was given, parse value to get settings fields
      self.function = DigitalInputFunction(self.value & 0b111111)
      self.mode = DigitalInputOutputMode(self.value >> 7 & 0b1)


  def __str__(self):
    return f'{self.mode.name} | {self.function.name} -> {self.value}'

@dataclass
class DigitalInputStates():
  value = int
  si_1: int = field(init=False)
  si_2: int = field(init=False)
  si_3: int = field(init=False)
  si_4: int = field(init=False)
  si_5: int = field(init=False)
  si_6: int = field(init=False)
  si_7: int = field(init=False)

  def __post_init__(self):
    self.si_7 = self.value >> 6 & 0b1
    self.si_6 = self.value >> 5 & 0b1
    self.si_5 = self.value >> 4 & 0b1
    self.si_4 = self.value >> 3 & 0b1
    self.si_3 = self.value >> 2 & 0b1
    self.si_2 = self.value >> 1 & 0b1
    self.si_1 = self.value & 0b1

@dataclass
class DigitalOutputStates():
  so_1: int
  so_2: int
  so_3: int
  value: int = field(init=False)
  def __post_init__(self):
    self.value = self.so_3<<2 | self.so_2<<1 | self.so_1

# Motion state
@dataclass
class MotionState():
  value: int
  is_normal: bool = field(init=False)
  is_enabled: bool = field(init=False)
  is_running: bool = field(init=False)
  command_completed: bool = field(init=False)
  path_completed: bool = field(init=False)
  homing_completed: bool = field(init=False)
  def __post_init__(self):
    self.is_normal = self.value >> 0 & 0b1
    self.is_enabled = self.value >> 1 & 0b1
    self.is_running = self.value >> 2 & 0b1
    self.command_completed = self.value >> 4 & 0b1
    self.path_completed = self.value >> 5 & 0b1
    self.homing_completed = self.value >> 6 & 0b1

# Control and status
class ControlWordFunction(IntEnum):
  RESET_CURRENT_ALARM = 0x1111
  RESET_HISTORY_ALARM = 0x1122
  SAVE_ALL_PARAMETERS_TO_EEPROM = 0x2211
  RESET_PARAMETERS_EXCLUDE_MOTOR_PARAMETERS = 0X2222
  RESET_ALL_PARAMETERS = 0X2233
  SAVE_ALL_MAPPINGS_INTO_EEPROM = 0X2244
  JOG_CW = 0X4001
  JOG_CCW = 0X4002

class StatusWordResponses(IntEnum):
  OK = 0x5555
  FAULT = 0xAAAA

@dataclass
class ControlWord():
  function: ControlWordFunction
  value: int = field(init=False)
  def __post_init__(self):
    self.value = self.function
  def __str__(self):
    return f'{self.function.name}'

@dataclass
class StatusWord():
  response: StatusWordResponses
  value: int = field(init=False)
  def __post_init__(self):
    self.value = self.response
  def __str__(self):
    return f'{self.response.name}'

# Alarms
@dataclass
class AlarmDetectionSelection():
  enable_over_current: bool
  enable_over_voltage: bool
  enable_adc_sampling_failure: bool
  enable_locked_shaft_alarm: bool
  enable_eeprom_alarm: bool
  enable_autotuning_alarm: bool
  value: int = field(init=False)
  def __post_init__(self):
    self.value = (self.enable_autotuning_alarm << 6 |
                  self.enable_eeprom_alarm << 5 |
                  self.enable_locked_shaft_alarm << 4 |
                  self.enable_adc_sampling_failure << 3 |
                  self.enable_over_voltage << 1 |
                  self.enable_over_current << 0)

class AlarmCode(IntEnum):
  OVER_CURRENT = 0x01
  OVER_VOLTAGE = 0x02
  ADC_SAMPLING_ERROR = 0x40
  SHAFT_LOCKING_ERROR = 0x80
  EEPROM_ERROR = 0x200
  AUTO_TUNING_ERROR = 0x100

@dataclass
class Alarm():
  code: AlarmCode
  troubleshooting: str = field(init=False)
  def __post_init__(self):
    if self.code == AlarmCode.OVER_CURRENT:
      self.troubleshooting = ('1. Restart the controller\n'
                              '2. If error persists, check if the motor is '
                              'short-circuited or disconnected from the '
                              'controller.')
    if self.code == AlarmCode.OVER_VOLTAGE:
      self.troubleshooting = ('1. Restart the controller\n'
                              '2. If error persists, check the voltage of the '
                              'power supply.')
    if self.code == AlarmCode.ADC_SAMPLING_ERROR:
      self.troubleshooting = ('1. Restart the controller\n'
                              '2. If error persists, there is a hardware '
                              'failure.')
    if self.code == AlarmCode.SHAFT_LOCKING_ERROR:
      self.troubleshooting = ('1. Check if the motor wire is broken.')
    if self.code == AlarmCode.EEPROM_ERROR:
      self.troubleshooting = ('1. Connect controller to the Leadshine software '
                              'and reset to factory parameters.\n2. If error '
                              'persists, there is a hardware failure.')

# Path control register
class PathControlRegisterCommand(IntEnum):
  PATH_MOTION = 0x10
  HOMING = 0x20
  SET_ZERO_POSITION = 0x21
  QUICK_STOP = 0x40

@dataclass
class PathControlRegisterWrite():
  command: PathControlRegisterCommand
  path: int = field(default=0)
  value: int = field(init=False)
  def __post_init__(self):
    if self.command == PathControlRegisterCommand.PATH_MOTION:
      self.command = self.command | (self.path & 0xF)
    self.value = self.command

class PathControlRegisterStatus(IntEnum):
  PATH_COMPLETE = 1
  NOT_RESPONDING = 2
  PATH_RUNNING = 3
  COMMAND_COMPLETED = 4
  POSITION_FOLLOW_ERROR = 5

@dataclass
class PathControlRegisterRead():
  value: int
  status: PathControlRegisterStatus = field(init=False)
  path: int = field(init=False)
  def __post_init__(self):
    if (self.value & 0xFFF0) == 0x000:
      self.status = PathControlRegisterStatus.PATH_COMPLETE
      self.path = self.value & 0x000F
    if (self.value & 0xFFF0) == 0x001 :
      self.status = PathControlRegisterStatus.NOT_RESPONDING
      self.path = self.value & 0x000F
    if (self.value == 0x0020):
      self.status = PathControlRegisterStatus.NOT_RESPONDING
    if (self.value == 0x0040):
      self.status = PathControlRegisterStatus.NOT_RESPONDING
    if (self.value & 0xFFF0) == 0x0100:
      self.status = PathControlRegisterStatus.PATH_RUNNING
      self.path = self.value & 0x000F
    if self.value == 0x0200:
      self.status = PathControlRegisterStatus.COMMAND_COMPLETED
    if (self.value & 0xFFF0) == 0x0200:
      self.status = PathControlRegisterStatus.POSITION_FOLLOW_ERROR
      self.path = self.value & 0x000F


# Homing
class HomingDirection(IntEnum):
  CCW = 0
  CW = 1

class HomingMode(IntEnum):
  DETECTED_BY_LIMIT_SWITCH = 0
  DETECTED_BY_HOME_SWITCH = 1

@dataclass
class HomingMode():
  direction: HomingDirection
  move_to_specified_point_after_homing: bool
  mode: HomingMode
  value: int = field(init=False)
  def __post_init__(self):
    self.value = (self.mode << 2 |
                  self.move_to_specified_point_after_homing << 1 |
                  self.direction)

@dataclass
class HomeSwitchPosition():
  value: int
  high_bits: int = field(init=False)
  low_bits: int = field(init=False)
  def __post_init__(self):
    self.high_bits = self.value >> 16 & 0xFFFF
    self.low_bits = self.value & 0xFFFF

@dataclass
class HomingStopPosition():
  value: int
  high_bits: int = field(init=False)
  low_bits: int = field(init=False)
  def __post_init__(self):
    self.high_bits = self.value >> 16 & 0xFFFF
    self.low_bits = self.value & 0xFFFF

# PR path
class PathOperationMode(IntEnum):
  NO_ACTION = 0
  POSITION_MODE = 1
  VELOCITY_MODE = 2
  HOMING_MODE = 3

class PathCoordinate(IntEnum):
  ABSOLUTE_POSITION = 0
  RELATIVE_POSITION = 1

@dataclass
class PathSettings():
  mode: PathOperationMode
  enable_interrupt: bool
  enable_overlapping: bool
  coordinate: PathCoordinate
  jump_to: int
  enable_jump: bool
  value: int = field(init=False)
  def __post_init__(self):
    self.value = (self.enable_jump << 14 |
                  self.jump_to << 8 & 0b11111 |
                  self.coordinate << 6 |
                  int(self.enable_overlapping) << 5 |
                  int(self.enable_interrupt) << 4 |
                  self.mode)

class CtrgTriggerMode(IntEnum):
  RISING_EDGE = 0
  DOUBLE_EDGE = 1
  
@dataclass
class PathGlobalControl():
  ctrg_trigger: CtrgTriggerMode = field(default=CtrgTriggerMode.RISING_EDGE)
  enable_soft_limit: bool = field(default=False)
  enable_autohoming_after_powerup: bool = field(default=False)
  value: int = None
  def __post_init__(self):
    if self.value == None:
      self.value = (self.enable_autohoming_after_powerup << 2 |
                    self.enable_soft_limit << 1 | self.ctrg_trigger)

CLASS_MAPPING = {
    "DigitalOutputSettings": DigitalOutputSettings,
}

def csv_to_list_dict(filename):
  """
  Reads a CSV file and converts it into a list of dictionaries.
  
  Args:
      filename (str): The path to the CSV file.
      
  Returns:
      list: A list where each element is a dictionary representing a row.
  """
  # Open the file in read mode
  with open(filename, mode='r', encoding='utf-8') as csvfile:
    # Create a DictReader object
    dict_reader = csv.DictReader(csvfile)
    # Convert the DictReader iterator to a list of dictionaries
    list_of_dicts = list(dict_reader)
  # Process list of dicts
  return list_of_dicts

filename = '/Users/nickwong-openshelf/Documents/embedded_dev/perpetual_os/perpetual_hardware/packages/hardware/src/hardware/data/iclrs_registers.csv'
registers = csv_to_list_dict(filename)

def get_register_by_name(name: str):
  found_register = next((d for d in registers if d.get('Name').strip().lower() == name.strip().lower()), None)
  return found_register

def setting_to_address_value(name: str, value: Union[int,dict]) -> tuple[int, int]:
  address = int(get_register_by_name(name)['Register Address'], 16)
  if type(value) == int:
    return (address, value)
  if type(value) == dict:
    register_type = get_register_by_name(name)['Type']
    MyClass = CLASS_MAPPING[register_type]
    instance = MyClass(**value)
    value = instance.value
    return (address, value)

'''
@dataclass
class RegisterParameter:
  register_address: int
  software_par_num: str
  name: str
  description: str
  range: str
  default_value: int
  unit: str
  min_value: int = field(init=False)
  max_value: int = field(init=False)

  def __post_init__(self):
    range = self.range.replace(' ','').split('-')
    self.min_value = int(range[0])
    self.max_value = int(range[1])

icl_rs_parameters = [
  # Register, Param No., Name, Description, Range, Default, Unit
  (0x0001, "Pr0.00", "Pulse/revolution", "Pulse count per revolution",
    "200-51200", 10000, "P/R"),
  (0x0005, "Pr0.02", "Control mode source", "Invalid", "0-10", 1, "--"),
  (0x0007, "Pr0.03", "Motor direction", "0: CW, 1: CCW", "0-1", 0, "--"),
  (0x0009, "Pr0.04", "Motor inductance", "Invalid", "0-10000", 1499, "0.001mH"),
  (0x000B, "Pr0.05", "Allowed max position following error",
    "It’s used to set the allowed max pulse count of position errors.",
    "0-65535", 4000, "CPR"),
  (0x000F, "Pr0.07", "Forced enable by software",
    ("Software forced enable has a higher priority than IO enable, and when "
    "this value is 0, the enable status of the drive only depends on the IO "
    "signal. When this value is 1, the motor is enabled regardless of the IO "
    "signal status."), "0-1", 0, "--"),
  # --------------------------------------------------- Verified above -----------------------------------------------

  (0x0051, "Pr1.00", "Position loop Kp",
    ("Increase this value will reduce position following error, but could result"
    " in motor vibration"), "0-3000", 25, "--"),
  (0x0053, "Pr1.01", "Velocity loop Ki", "Velocity loop integral gain",
    "0-3000", 3, "--"),
  (0x0055, "Pr1.02", "Velocity loop Kp", "Velocity loop proportional gain",
    "0-3000", 25, "--"),
  (0x0065, "Pr1.10", "Position loop KpH", "Usually keep default",
    "0-3000", 0, "--"),

  (0x00A1, "Pr2.00", "Command filter time", "Filter time for internal command", "0-512", 15, "0.1ms"),
  (0x00A3, "Pr2.01", "Velocity switching: open→closed", "Mode switching velocity threshold", "0-200", 18, "0.1RPS"),
  (0x00A5, "Pr2.02", "Velocity switching: closed→open", "Mode switching velocity threshold", "0-200", 12, "0.1RPS"),
  (0x00A7, "Pr2.03", "Open→closed loop delay", "Delay in ms", "0-32767", 5, "ms"),
  (0x00A9, "Pr2.04", "Closed→open loop delay", "Delay in ms", "0-32767", 250, "ms"),
  (0x0145, "Pr4.02", "SI1 input function", "DI1 digital input config", "0-65535", 136, "--"),
  (0x0147, "Pr4.03", "SI2 input function", "DI2 input config", "0-65535", 0, "--"),
  (0x0149, "Pr4.04", "SI3 input function", "DI3 input config", "0-65535", 0, "--"),
  (0x014B, "Pr4.05", "SI4 input function", "DI4 input config", "0-65535", 0, "--"),
  (0x014D, "Pr4.06", "SI5 input function", "DI5 input config", "0-65535", 0, "--"),
  (0x014F, "Pr4.07", "SI6 input function", "DI6 input config", "0-65535", 0, "--"),
  (0x0151, "Pr4.08", "SI7 input function", "DI7 input config", "0-65535", 0, "--"),
  (0x0157, "Pr4.11", "SO1 output function", "DO1 output config", "0-65535", 0, "--"),
  (0x0159, "Pr4.12", "SO2 output function", "DO2 output config", "0-65535", 0, "--"),
  (0x015B, "Pr4.13", "SO3 output function", "DO3 output config", "0-65535", 0, "--"),
  (0x0167, "Pr4.19", "Delay of brake released", "Keep default normally", "0-1500", 250, "ms")
  (0x0169, "Pr4.20", "Delay of brake locked", "Keep default normally", "0-1500", 250, "ms")
  (0x016B, "Pr4.21", "Threshold value of brake locking velocity", "Keep default normally", "0-500", 10, "ms")

  (0x016D, "Pr4.22", "Alarm detection", "Bitmask of active alarm types", "0-65535", 0, "--"),
  (0x0171, "Pr4.24", "In-position pulse threshold", "Distance for in-position signal", "0-1500", 200, "CPR"),
  (0x0177, "Pr4.27", "Bus voltage", "Measured bus voltage", "0-65535", 0, "0.1V"),
  (0x0179, "Pr4.28", "Digital input state", "Bit0-Bit6: DI1-DI7", "0-65535", 0, "--"),
  (0x017B, "Pr4.29", "Digital output state", "Bit0-Bit2: DO1-DO3", "0-65535", 0, "--"),
  (0x0187, "Pr4.35", "DIP switches status", "Read DIP switch setting", "0-65535", 0, "--"),
  (0x0191, "Pr5.00", "Peak current", "Phase current *1.4 approx.", "0-30/70", 10, "0.1A"),
  (0x0193, "Pr5.01", "Closed loop holding current %", "Prevent overheating", "0-50", 50, "%"),
  (0x0195, "Pr5.02", "Open loop holding current %", "Prevent overheating", "0-50", 50, "%"),
  (0x0197, "Pr5.03", "Shaft lock current %", "Power on shaft lock setting", "0-100", 100, "--"),
  (0x0199, "Pr5.04", "Shaft lock duration", "Motor lock timeout", "0-1500", 200, "1ms"),
  (0x019F, "Pr5.07", "Shaft lock current rise time", "Ramp duration", "1-60", 1, "100ms"),
  (0x01A5, "Pr5.10", "Max stop time", "Max time before stop", "100-1000", 1000, "ms"),
  (0x01AB, "Pr5.13", "Auto-tuning at power-on", "Enable autotune on boot", "0-1", 1, "--"),
  (0x01BD, "Pr5.22", "RS485 baud rate", "Communication speed", "0-6", 4, "--"),
  (0x01BF, "Pr5.23", "RS485 ID", "Device ID", "0-127", 1, "--"),
  (0x01C1, "Pr5.24", "RS485 data type", "Data+parity+stop config", "0-11", 4, "--"),
  (0x01C3, "Pr5.25", "RS485 control word", "Control bits", "0-32767", 0, "--"),
  (0x01C4, "Pr5.26", "Comm bit delay", "Bit delay", "0-100", 35, "bit"),
  (0x01D1, "Pr5.32", "Switching time to standby", "Idle timeout", "10-65535", 200, "ms"),
  (0x01D3, "Pr5.33", "Standby current %", "Current reduction in standby", "0-100", 50, "%"),
  (0x01E1, "Pr6.00", "JOG velocity", "Speed set via RS485", "0-5000", 60, "rpm"),
  (0x01E3, "Pr6.01", "Interval", "Jog delay", "0-10000", 100, "ms"),
  (0x01E5, "Pr6.02", "Running times", "Jog loop count", "0-30000", 1, "--"),
  (0x01E7, "Pr6.03", "Accel/Decel time", "Jog acceleration", "0-10000", 200, "--"),
  (0x01FF, "Pr6.15", "Version info", "Firmware version", "0-65535", 0, "--"),
  (0x0201, "Pr6.16", "Firmware info", "Firmware build details", "0-65535", 0, "--"),
  (0x1003, None, "Motion status", "Bitwise flags: Fault, Enable, Running, etc.", "--", None, "--"),
  (0x1801, None, "Control word", "Command triggers via write", "--", None, "--"),
  (0x1901, None, "Save status word", "0x5555 = OK, 0xAAAA = fail", "--", None, "--"),
  (0x2203, None, "Current alarm", "Alarm code: overcurrent, etc.", "--", None, "--"),
  (0x600A, None, "Homing mode", "bit0: direction (0=CCW,1=CW), bit1: move to position, bit2: type (0=limit,1=home switch)", "--", "bitfield", "--"),
  (0x600B, None, "Home switch position (high)", "High 16 bits of home switch absolute position", "pulses", "--", "--"),
  (0x600C, None, "Home switch position (low)", "Low 16 bits of home switch absolute position", "pulses", "--", "--"),
  (0x600D, None, "Homing stop position (high)", "High 16 bits of final position after homing", "pulses", "--", "--"),
  (0x600E, None, "Homing stop position (low)", "Low 16 bits of final position after homing", "pulses", "--", "--"),
  (0x600F, None, "Homing high velocity", "First segment homing velocity", "rpm", "0–5000", "--"),
  (0x6010, None, "Homing low velocity", "Second segment precise homing velocity", "rpm", "0–5000", "--"),
  (0x6011, None, "Homing acceleration", "Acceleration during homing", "ms/1000rpm", "--", "--"),
  (0x6012, None, "Homing deceleration", "Deceleration during homing", "ms/1000rpm", "--", "--"),
  # (0x6002, None, "Set current position to 0", "Write 0x21 to set current motor position to zero", "--", "--", "--"),
  (0x6000, "Pr8.00", "PR global control", "--", "0-0b1111", 0b0000, "bitfield"),
  # (0x6000, None, "Auto-homing enable", "bit2 = 1 enables auto-homing at power-on", "--", "bitfield", "--"),
  (0x602D, None, "Position Low (Actual)", "", "--", "bitfield", "--"),
  (0x602C, None, "Position High (Actual)", "", "--", "bitfield", "--"),
  (0x6200, "Pr9.00", "Motion of Path 0", "Path config: type, interrupt, jump, relative", "--", None, "--"),
  (0x6201, "Pr9.01", "Position High", "High 16 bits of position setpoint", "--", None, "--"),
  (0x6202, "Pr9.02", "Position Low", "Low 16 bits of position setpoint", "--", None, "--"),
  (0x6203, "Pr9.03", "Velocity", "Set velocity", "--", None, "rpm"),
  (0x6204, "Pr9.04", "Acceleration", "Acceleration time", "--", None, "ms/1000rpm"),
  (0x6205, "Pr9.05", "Deceleration", "Deceleration time", "--", None, "ms/1000rpm"),
  (0x6206, "Pr9.06", "Pause Time", "Pause duration after move", "--", None, "ms"),
  (0x6207, "Pr9.07", "Special Parameter", "Path 0 links to Pr8.02, others reserved", "--", None, "--")
]

'''