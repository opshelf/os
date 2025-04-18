/*
class ToolStatus(BaseModel):
    gripper: AxisStatus
    z_axis: AxisStatus


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class MoveCommand(BaseModel):
    command: Literal["move"]
    position: float


class MoveRelativeCommand(BaseModel):
    command: Literal["move_relative"]
    position: float


class SetJawSizeCommand(BaseModel):
    command: Literal["set_jaw_size"]
    size: float


ToolCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveRelativeCommand,
        SetJawSizeCommand,
    ],
    Field(discriminator="command"),
]
*/
