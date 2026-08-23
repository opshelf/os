from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.models import Robot
from openshelf_os.config.settings import (
    AuthSettings,
    DemoSettings,
    OverheadCameraSettings,
    OverviewCameraSettings,
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
    description: str | None = None
    robot: Robot
    status: Literal["idle", "running", "error"] = "idle"

    storage: StorageSettings = Field(default_factory=StorageSettings)
    sizes: SizesSettings = Field(default_factory=SizesSettings)
    printer: PrinterSettings = Field(default_factory=PrinterSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    tool_camera: ToolCameraSettings = Field(default_factory=ToolCameraSettings)
    overhead_camera: OverheadCameraSettings = Field(default_factory=OverheadCameraSettings)
    overview_left_camera: OverviewCameraSettings = Field(default_factory=OverviewCameraSettings)
    overview_right_camera: OverviewCameraSettings = Field(default_factory=OverviewCameraSettings)
    scale: ScaleAppSettings = Field(default_factory=ScaleAppSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)  # type: ignore[arg-type]
    robot_app: RobotAppSettings = Field(default_factory=RobotAppSettings)
    demo: DemoSettings = Field(default_factory=DemoSettings)

    @model_validator(mode="before")
    @classmethod
    def _migrate_robot_enabled(cls, values: Any) -> Any:
        if isinstance(values, dict) is False:
            return values

        if "robot_app" in values:
            return values

        robot = values.get("robot")
        if isinstance(robot, dict) is False:
            return values

        robot_enabled = robot.get("enabled")
        if robot_enabled is None:
            return values

        data = dict(values)
        robot_data = dict(robot)
        robot_data.pop("enabled", None)
        data["robot"] = robot_data
        data["robot_app"] = {"enabled": robot_enabled}
        return data


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
