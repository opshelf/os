from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.models import (
    Motor,
    Motors,
    PoeCamera,
    UsbCamera,
    LaserConfig,
    Robot,
)
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
from openshelf_os.config.types import TwoInt


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

    # ----------- Convenience Getters (typed) -----------

    def get_status(self) -> Literal["idle", "running", "error"]:
        return self.status

    def get_parameter(self, key: str) -> Any:
        params = self.robot.tool.parameters
        if not hasattr(params, key):
            raise KeyError(f"Parameter '{key}' not found")
        return getattr(params, key)

    def get_pxmm(self) -> float:
        return self.robot.tool.parameters.PXMM

    def get_pymm(self) -> float:
        return self.robot.tool.parameters.PYMM

    def get_idle_position(self) -> TwoInt:
        return self.robot.tool.parameters.IDLE_POSITION

    def list_motors(self, section: Literal["tool", "io"]) -> List[str]:
        motors: Motors = getattr(self.robot, section).motors
        return motors.names()

    def get_motor(self, section: Literal["tool", "io"], *, name: Optional[str] = None,
                  motor_id: Optional[int] = None) -> Motor:
        motors: Motors = getattr(self.robot, section).motors
        if (name is None) == (motor_id is None):
            raise ValueError("Provide exactly one of name or motor_id")
        assert motor_id is not None  # validated by the mutual-exclusion check above
        return motors.by_name(name) if name is not None else motors.by_id(motor_id)

    def get_poe_camera(self) -> PoeCamera:
        return self.robot.tool.poe_camera

    def get_usb_camera(self) -> UsbCamera:
        return self.robot.io.usb_camera

    def get_tool_laser(self) -> LaserConfig:
        return self.robot.tool.laser

    def get_grid(self) -> Dict[str, Any]:
        return self.robot.grid.model_dump()

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
