from pydantic import BaseModel, Field
from enum import IntEnum

class ControlRegister(BaseModel):
    """
    The electric gripper shall be enabled before controlling.
    0x0: stop the electric gripper,
    0x1: enable the electric gripper
    Note: the rACT bit remains unchanged in any other operation.
    Clear the rACT bit to restart the electric gripper and clear all the fault status.
    When enabling, make sure that there are no obvious obstructions in the slide rail that
    could cause the stroke to be reduced
    """

    enable: bool
    """
    Set the working mode of the electric parallel gripper
    0x0: in the parameter control mode, and the position, speed and force parameters under
    the current command should be set.
    0x1: In no input parameter control mode, reference register 0x03E8 high byte for fixed
    parameter reference mode.
    """
    mode: bool
    """
    Control the electric gripper to move according to the parameters set.
    0x0: stop
    0x1: move to the target position
    Note: in case that this bit is not set, this device only executes the enable command, not
    acting;
    When rMODE=0X0, the parameters are set by high byte of 0X03E9 and 0X03EA;
    When rMODE=0x1, the parameter is set by the parameter corresponding to the high
    byte of 0x03E8.
    """
    move_to_targret: bool
    """
    This bit controls the electric gripper to open slowly, until all the motion shafts reach
    their mechanical limit. The electric gripper will send a fault signal after completing all
    the motions. Before executing other actions, this fault signal shall be cleared. This
    action executes all the commands except rACT, which can help to judge whether the
    electric gripper is normal.
    0x0: normal
    0x1: emergency automatic release
    Note: 1) This command must be set to 0x0 and then 0x1 before it can be executed.
    (2) Generally, it is operated only to check whether the gripper works properly or not
    after an emergency in the robotic arm or equipment, and the command is not executed
    in general.
    """
    automatic_inspection: bool
    """
    ARD: moving direction of patrol inspection command. rARD bit shall be set before or at the same time as rA TR bit, as the motion direction is required to be set when starting the automatic release
    0x0: the patrol inspection direction is to close the electric gripper;
    0x1: the patrol inspection direction is to open the electric gripper
    """
    automatic_patrol_inspection: bool

    def to_u8(self) -> int:
        ret = 0
        if self.enable:
            ret |= 0x01
        if self.mode:
            ret |= 0x02
        if self.move_to_targret:
            ret |= 0x08
        if self.automatic_inspection:
            ret |= 0x10
        if self.automatic_patrol_inspection:
            ret |= 0x20
        return ret


class ExecuteControlMovement(BaseModel):
    force: int
    speed: int
    position: int

class GSTA(IntEnum):
    RESET_OR_INSPECTION = 0
    ACTIVATING = 1
    RESERVED = 2
    ACTIVATED = 3


class GOBJ(IntEnum):
    MOVING_TO_POSITION = 0
    INNER_SUPPORT = 1
    OUTER_CLIP = 2
    NO_OBJECT_DETECTED = 3


class GripperStatus(BaseModel):
    gACT: bool = Field(False, description="Bit 0 - Gripper is enabled (rACT)")
    gDropSta: bool = Field(False, description="Bit 1 - Workpiece drop status")
    gMODE: bool = Field(
        False, description="Bit 2 - Operating mode (0=param control, 1=no-param mode)"
    )
    gGTO: bool = Field(
        False, description="Bit 3 - Action status (0=stop/reset, 1=going to target)"
    )
    gSTA: GSTA = Field(..., description="Bits 4-5 - Current status")
    gOBJ: GOBJ = Field(..., description="Bits 6-7 - Object detection state")

    @classmethod
    def from_u8(cls, value: int) -> "GripperStatus":
        return cls(
            gACT=bool(value & (1 << 0)),
            gDropSta=bool(value & (1 << 1)),
            gMODE=bool(value & (1 << 2)),
            gGTO=bool(value & (1 << 3)),
            gSTA=GSTA((value >> 4) & 0b11),
            gOBJ=GOBJ((value >> 6) & 0b11),
        )


class GripperFaultStatus(BaseModel):
    control_command_error: bool = Field(
        False, description="Bit 0 - Control command error"
    )
    communication_fault: bool = Field(
        False, description="Bit 1 - Communication fault within 1s"
    )
    overcurrent_fault: bool = Field(False, description="Bit 2 - Current abnormality")
    voltage_fault: bool = Field(
        False, description="Bit 3 - Voltage abnormality (outside 20V–30V)"
    )
    enabling_fault: bool = Field(False, description="Bit 4 - Enabling error")
    overtemperature_fault: bool = Field(
        False, description="Bit 5 - Overtemperature (≥85°C)"
    )
    self_fault: bool = Field(False, description="Bit 6 - Product self-fault")

    @classmethod
    def from_u8(cls, value: int) -> "GripperFaultStatus":
        return cls(
            control_command_error=bool(value & (1 << 0)),
            communication_fault=bool(value & (1 << 1)),
            overcurrent_fault=bool(value & (1 << 2)),
            voltage_fault=bool(value & (1 << 3)),
            enabling_fault=bool(value & (1 << 4)),
            overtemperature_fault=bool(value & (1 << 5)),
            self_fault=bool(value & (1 << 6)),
        )


class VoltageTemperature(BaseModel):
    voltage: int
    temperature: int


class SpeedForce(BaseModel):
    speed: int
    force: int


class FaultPosition(BaseModel):
    fault: GripperFaultStatus
    position: int


