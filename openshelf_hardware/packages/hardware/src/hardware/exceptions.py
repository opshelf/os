class HardwareError(Exception):
    """Base exception for all hardware-related errors."""
    pass

class CommunicationError(HardwareError):
    """Base class for communication-related errors."""
    pass

class ModbusError(CommunicationError):
    """Modbus communication errors."""
    pass

class ModbusTimeoutError(ModbusError):
    """Modbus operation timed out."""
    pass

class ModbusConnectionError(ModbusError):
    """Failed to establish Modbus connection."""
    pass

class CameraError(HardwareError):
    """Base class for camera-related errors."""
    pass

class CameraConnectionError(CameraError):
    """Camera connection or initialization failed."""
    pass


class CameraNotPresentError(CameraConnectionError):
    """Optional camera hardware was not detected."""
    pass

class CameraConfigError(CameraError):
    """Camera configuration error."""
    pass

class ImageError(CameraError):
    """Image capture or processing error."""
    pass

class ToFError(HardwareError):
    """Base class for time-of-flight sensor errors."""
    pass

class ToFConnectionError(ToFError):
    """Failed to establish time-of-flight sensor connection."""
    pass

class ToFReadError(ToFError):
    """Failed to read a time-of-flight sensor measurement."""
    pass

class ToFConfigError(ToFError):
    """Time-of-flight sensor configuration error."""
    pass
class MotorError(HardwareError):
    """Base class for motor-related errors."""
    pass

class MotorTimeoutError(MotorError):
    """Motor operation timed out."""
    pass

class MotorLimitError(MotorError):
    """Motor hit a limit switch or exceeded bounds."""
    pass

class MotorConfigError(MotorError):
    """Error in motor configuration."""
    pass

class PrinterError(HardwareError):
    """Base class for printer-related errors."""
    pass

class PrinterConnectionError(PrinterError):
    """Printer connection or initialization failed."""
    pass
