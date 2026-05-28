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

class ImageProcessingError(ImageError):
    """Error during image processing or analysis."""
    pass

class CalibrationError(CameraError):
    """Camera calibration error."""
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

class MotorMovementError(MotorError):
    """Error during motor movement."""
    pass

class MotorStateError(MotorError):
    """Invalid motor state transition."""
    pass


class PrinterError(HardwareError):
    """Base class for printer-related errors."""
    pass

class PrinterConnectionError(PrinterError):
    """Printer connection or initialization failed."""
    pass

class PrinterIOError(PrinterError):
    """Printer I/O error."""
    pass
