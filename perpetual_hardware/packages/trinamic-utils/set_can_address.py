from pytrinamic.connections import ConnectionManager
from pytrinamic.modules import TMCM1240
import time

"""
X: 100
Y: 101
Z: 102
B: 103
I: 104
J: 105
K: 106
GRIPPER: 107

class CanMessageIds(int, Enum):
    PNEUMATIC_NODE = 5
    TOOL_NODE = 6
    X_AXIS = 100
    Y_AXIS = 101
    Z_AXIS = 102
    INPUT_FUNNEL = 110
    OPPOSING_FUNNEL = 111
    SLOT_1 = 112
    SLOT_2 = 113
    SLOT_3 = 114
    INPUT_CONVEYOR = 130
    DIVERTER = 131
    OUTPUT_TRANSFER_CONVEYOR = 132
    ACTIVE_WALL = 133
    OUTPUT_CONVEYOR = 134
    IO_IN_CONVEYOR = 135
    IO_OUT_CONVEYOR = 136
    OUTPUT_BOX = 137

"""

if __name__ == "__main__":
    with ConnectionManager().connect() as my_interface:
        CAN_ID = 112
        module = TMCM1240(my_interface)
        print(module)
        print("CURRENT RECEIVE ID: ", module.get_global_parameter(module.GP0.CANreceiveID, 0))
        print("CURRENT SEND ID: ", module.get_global_parameter(module.GP0.CANsendID, 0))
        print("CURRENT BIT RATE: ", module.get_global_parameter(module.GP0.CANBitRate, 0))
        module.set_global_parameter(module.GP0.CANreceiveID, 0, CAN_ID)
        module.set_global_parameter(module.GP0.CANsendID, 0, CAN_ID)
        module.set_global_parameter(module.GP0.CANBitRate, 0, 7)
        print("NEW RECEIVE ID: ", module.get_global_parameter(module.GP0.CANreceiveID, 0))
        print("NEW SEND ID: ", module.get_global_parameter(module.GP0.CANsendID, 0))
        print("NEW BIT RATE: ", module.get_global_parameter(module.GP0.CANBitRate, 0))
