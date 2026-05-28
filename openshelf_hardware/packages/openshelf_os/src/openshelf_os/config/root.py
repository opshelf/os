from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.models import Robot
from openshelf_os.config.settings import (
    AuthSettings,
    CameraAppSettings,
    DemoSettings,
    OverheadCameraSettings,
    PrinterSettings,
    RobotAppSettings,
    ScaleAppSettings,
    ScannerSettings,
    SizesSettings,
    StorageSettings,
    ToolCameraSettings,
)


class RootConfig(BaseModel):
    name: str
    version: str
    description: Optional[str] = None
    robot: Robot
    status: Literal["idle", "running", "error"] = "idle"

    storage: StorageSettings = Field(default_factory=StorageSettings)
    sizes: SizesSettings = Field(default_factory=SizesSettings)
    printer: PrinterSettings = Field(default_factory=PrinterSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    camera: CameraAppSettings = Field(default_factory=CameraAppSettings)
    tool_camera: ToolCameraSettings = Field(default_factory=ToolCameraSettings)
    overhead_camera: OverheadCameraSettings = Field(default_factory=OverheadCameraSettings)
    scale: ScaleAppSettings = Field(default_factory=ScaleAppSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)  # type: ignore[arg-type]
    robot_app: RobotAppSettings = Field(default_factory=RobotAppSettings)
    demo: DemoSettings = Field(default_factory=DemoSettings)

    @model_validator(mode="before")
    @classmethod
    def _migrate_robot_enabled(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(values, dict) and "robot_app" not in values:
            if "robot" in values and isinstance(values["robot"], dict):
                robot_enabled = values["robot"].pop("enabled", None)
                if robot_enabled is not None:
                    values["robot_app"] = {"enabled": robot_enabled}
        return values


    def get(self, dotted_path: str, default: Any = ...) -> Any:
        cur: Any = self
        parts = dotted_path.split(".")
        try:
            for p in parts:
                if isinstance(cur, BaseModel):
                    cur = getattr(cur, p)
                elif isinstance(cur, dict):
                    cur = cur[p]
                else:
                    cur = getattr(cur, p)
            return cur
        except Exception:
            if default is ...:
                raise KeyError(f"path '{dotted_path}' not found")
            return default
