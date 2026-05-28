"""OpenShelf OS Exception Classes"""

class OpenshelfError(Exception):
    """Base exception for all OpenShelf OS errors."""
    pass

class MotorError(OpenshelfError):
    """Exception raised for motor-related errors."""
    pass

class CameraError(OpenshelfError):
    """Exception raised for camera-related errors."""
    pass

class ConfigError(OpenshelfError):
    """Exception raised for configuration-related errors."""
    pass

class ToolError(OpenshelfError):
    """Exception raised for tool-related errors."""
    pass

class GripperError(ToolError):
    """Exception raised for gripper-related errors."""
    pass

class MovementError(ToolError):
    """Exception raised for movement-related errors."""
    pass
class IOBoxError(OpenshelfError):
    """Exception raised for IO box-related errors."""
    pass


class OperationTimeoutError(OpenshelfError):
    """Exception raised when an operation exceeds its timeout."""
    pass


class MotorWaitTimeoutError(OperationTimeoutError):
    """Exception raised when motor wait operation times out."""
    pass
