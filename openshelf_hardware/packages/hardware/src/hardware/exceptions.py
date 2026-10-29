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

class ModbusTransientNoResponseError(ModbusError):
    """A request the drive never answered, retried in vain on a port that still reconnects.

    A dropout on a shared RTU line rather than a device fault. It is marked with the
    `transient` attribute the hardware layer checks (hardware.controller.is_no_response_error) and it
    keeps pymodbus' own no-response text as its message, so the report built from it is one the
    Sentry hooks in openshelf_os drop. Any Modbus error the drive actually answered -- an
    exception response, a short payload -- stays a plain ModbusError and stays reportable.
    """

    transient = True

class CameraError(HardwareError):
    """Base class for camera-related errors."""
    pass

class CameraConnectionError(CameraError):
    """Camera connection or initialization failed."""
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

class GpioError(HardwareError):
    """Base class for GPIO-related errors."""
    pass


class GpioConfigError(GpioError):
    """GPIO configuration error."""
    pass


class GpioConnectionError(GpioError):
    """Failed to initialize GPIO hardware."""
    pass


class GpioReadError(GpioError):
    """Failed to read a GPIO pin."""
    pass


class GpioWriteError(GpioError):
    """Failed to write a GPIO pin."""
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
