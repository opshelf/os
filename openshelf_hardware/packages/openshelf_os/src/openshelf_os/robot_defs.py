
from datetime import datetime, timezone
from typing import Optional, List, Any
from pydantic import BaseModel, Field, TypeAdapter

from openshelf_os.robot import RobotCommands, RobotStatus

class WorkerError(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_type: str
    message: str
    context: Optional[dict[str, Any]] = None
    stack_trace: Optional[str] = None

class QueuedOperation(BaseModel):
    command: RobotCommands
    timestamp: datetime
    location: Optional[dict[str, Any]] = None
    item_data: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]
    error_queue: List[WorkerError] = Field(default_factory=list)
    is_crashed: bool = False
    crash_reason: Optional[str] = None
    needs_home: bool = False
    needs_home_reason: Optional[str] = None
    needs_home_axes: List[str] = Field(default_factory=list)

class WorkerStatusWithQueue(WorkerStatus):
    queued_operations: List[QueuedOperation] = Field(default_factory=list)
    queue_length: int = 0

RobotCommandAdapter = TypeAdapter[RobotCommands](RobotCommands)
