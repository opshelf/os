
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Any
from pydantic import BaseModel, Field

from openshelf_os.robot.commands import RobotCommands
from openshelf_os.robot.status import RobotStatus


@dataclass(frozen=True)
class CrashReportAttachment:
    filename: str
    data: bytes
    content_type: str


@dataclass
class CrashReportArtifacts:
    attachments: list[CrashReportAttachment] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


class WorkerError(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_type: str
    message: str
    context: Optional[dict[str, Any]] = None
    stack_trace: Optional[str] = None


class QueuedOperation(BaseModel):
    command: RobotCommands
    timestamp: datetime
    admission_generation: int = Field(default=0, exclude=True)
    command_id: Optional[str] = None
    visibility: Optional[str] = None
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
    crash_context: Optional[dict[str, Any]] = None
    is_emergency_stopped: bool = False
    is_emergency_stop_active: bool = False
    emergency_stop_reason: Optional[str] = None
    needs_home: bool = False
    needs_home_reason: Optional[str] = None
    needs_home_axes: List[str] = Field(default_factory=list)


class WorkerStatusWithQueue(WorkerStatus):
    queued_operations: List[QueuedOperation] = Field(default_factory=list)
    queue_length: int = 0
