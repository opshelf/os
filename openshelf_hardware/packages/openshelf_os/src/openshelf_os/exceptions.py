"""OpenShelf OS Exception Classes"""

# Failure codes shared by the worker event payload, the crash status the operator UI reads, and
# the tray workflow recovery rules. Each one tells the operator where to look for the tray.
GRIP_PICK_FAILED_CODE = "grip_pick_failed"
GRIP_DROPPED_CODE = "grip_dropped"
GRIP_IO_BOX_FAILED_CODE = "grip_io_box_failed"
# The gripper still touched the tray after a release, so the tool stopped instead of withdrawing.
GRIP_RELEASE_FAILED_CODE = "grip_release_failed"


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


class GripVerificationError(GripperError):
    """Gripper object feedback did not confirm the expected object during a pick or return.

    The gantry never loses position on this fault, so only the tool motors need homing, plus
    the IO box door when the tray was in the IO box.
    failure_code says where the tray was last held, which decides whether the command can be
    cancelled with inventory untouched and what the operator is asked to confirm.
    """

    def __init__(self, message: str, *, failure_code: str = GRIP_DROPPED_CODE) -> None:
        super().__init__(message)
        self.failure_code = failure_code

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
