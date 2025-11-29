"""Perpetual OS1 Exception Classes"""

class PerpetualError(Exception):
    """Base exception for all Perpetual OS errors."""
    pass

class MotorError(PerpetualError):
    """Exception raised for motor-related errors."""
    pass

class CameraError(PerpetualError):
    """Exception raised for camera-related errors."""
    pass

class ConfigError(PerpetualError):
    """Exception raised for configuration-related errors."""
    pass

class ToolError(PerpetualError):
    """Exception raised for tool-related errors."""
    pass

class GripperError(ToolError):
    """Exception raised for gripper-related errors."""
    pass

class MovementError(ToolError):
    """Exception raised for movement-related errors."""
    pass

class StorageError(PerpetualError):
    """Exception raised for storage-related errors."""
    pass

class IOBoxError(PerpetualError):
    """Exception raised for IO box-related errors."""
    pass


class OperationTimeoutError(PerpetualError):
    """Exception raised when an operation exceeds its timeout."""
    pass


class ScanTimeoutError(OperationTimeoutError):
    """Exception raised when camera scanning operation times out."""
    pass


class MotorWaitTimeoutError(OperationTimeoutError):
    """Exception raised when motor wait operation times out."""
    pass