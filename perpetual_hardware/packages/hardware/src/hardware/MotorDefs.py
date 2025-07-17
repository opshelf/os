from collections import OrderedDict

TRIGGER         = 0x6002         

# Full list of parsed register-parameter pairs from the datasheet, formatted for readability and export
motor_registers = [
    # Format: Register, Param No., Name, Description, Range, Default, Unit
    (0x0001, "Pr0.00", "Pulse/revolution", "Pulse count per revolution", "200-51200", 10000, "P/R"),
    (0x0003, "Pr0.01", "Control Mode", "0=open loop, 2=closed loop", "0-255", 2, "--"),
    (0x0005, "Pr0.02", "Control mode source", "Invalid", "0-10", 1, "--"),
    (0x0007, "Pr0.03", "Motor direction", "0: CW, 1: CCW", "0-1", 0, "--"),
    (0x0009, "Pr0.04", "Motor inductance", "Invalid", "0-10000", 1499, "0.001mH"),
    (0x000B, "Pr0.05", "Allowed max position following error", "Set max allowed pulse count for error", "0-65535", 4000, "CPR"),
    (0x000F, "Pr0.07", "Forced enable by software", "Overrides IO enable", "0-1", 0, "--"),
    (0x0051, "Pr1.00", "Position loop Kp", "Position loop gain", "0-3000", 25, "--"),
    (0x0053, "Pr1.01", "Velocity loop Ki", "Velocity loop integral gain", "0-3000", 3, "--"),
    (0x0055, "Pr1.02", "Velocity loop Kp", "Velocity loop proportional gain", "0-3000", 25, "--"),
    (0x0065, "Pr1.10", "Position loop KpH", "Usually keep default", "0-3000", 0, "--"),
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
    (0x6200, "Pr9.00", "Motion of Path 0", "Path config: type, interrupt, jump, relative", "--", None, "--"),
    (0x6201, "Pr9.01", "Position High", "High 16 bits of position setpoint", "--", None, "--"),
    (0x6202, "Pr9.02", "Position Low", "Low 16 bits of position setpoint", "--", None, "--"),
    (0x6203, "Pr9.03", "Velocity", "Set velocity", "--", None, "rpm"),
    (0x6204, "Pr9.04", "Acceleration", "Acceleration time", "--", None, "ms/1000rpm"),
    (0x6205, "Pr9.05", "Deceleration", "Deceleration time", "--", None, "ms/1000rpm"),
    (0x6206, "Pr9.06", "Pause Time", "Pause duration after move", "--", None, "ms"),
    (0x6207, "Pr9.07", "Special Parameter", "Path 0 links to Pr8.02, others reserved", "--", None, "--")
]

# the other 15 paths are exactly the same 8 register structure

# Convert to list of dicts for exporting or interactive usage
register_dict_list = [
    OrderedDict([
        ("Register", f"0x{reg:04X}"),
        ("Param No.", param),
        ("Name", name),
        ("Description", desc),
        ("Range", range_),
        ("Default", default),
        ("Unit", unit),
    ])
    for reg, param, name, desc, range_, default, unit in motor_registers
]

def get_register_by_name(param_name: str) -> int:
    for entry in register_dict_list:
        if entry.get("Name", "").strip().lower() == param_name.strip().lower():
            return int(entry.get("Register"), 16)
    return None

def rev_to_pulse(rev: int):
    pulse_rev = 10000 # todo have this read from the motor
    val = int(rev*pulse_rev)
    top_bytes = (val >> 16) & 0xFFFF
    bottom_bytes = val & 0xFFFF
    return top_bytes, bottom_bytes

def mm_to_rev(mm: int, mpr: float):
    val = int(mm / mpr)
    return val

