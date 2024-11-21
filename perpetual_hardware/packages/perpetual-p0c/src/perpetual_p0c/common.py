from hardware_common.stepper_motor import StepperMotorConfig
from pydantic import BaseModel
from enum import Enum
from can import Bus, BusABC, ThreadSafeBus


class StorageModuleSide(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class CanAxisConfiguration(BaseModel):
    can_id: int
    configuration: StepperMotorConfig
    name: str
    axis_type: str


class CanMessageIds(int, Enum):
    PNEUMATIC_NODE = 5
    TOOL_NODE = 6
    X_AXIS = 100
    Y_AXIS = 101
    Z_AXIS = 105
    INPUT_FUNNEL = 110
    OPPOSING_FUNNEL = 111
    SLOT_1 = 112
    SLOT_2 = 113
    SLOT_3 = 114
    INPUT_CONVEYOR = 130
    DIVERTER = 131
    OUTPUT_TRANSFER_CONVEYOR = 132
    ACTIVE_WALL = 133


def get_bus() -> BusABC:
    return ThreadSafeBus(interface="pcan", channel="PCAN_USBBUS1", bitrate=1000000, fd=False)
