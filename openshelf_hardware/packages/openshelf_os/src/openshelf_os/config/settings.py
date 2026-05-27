from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class StorageSettings(BaseModel):
    filename: str = "data/storage.json"


class SizesSettings(BaseModel):
    filename: str = "data/size.json"


class PrinterSettings(BaseModel):
    enabled: bool = False
    queue: Optional[str] = None


class ScannerSettings(BaseModel):
    enabled: bool = False


class CameraAppSettings(BaseModel):
    enabled: bool = False


class ToolCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class OverheadCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class AuthSettings(BaseModel):
    enabled: bool = True
    require_auth: bool = True
    jwt_secret: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(30, gt=0)
    jwt_refresh_expire_days: int = Field(7, gt=0)
    dev_mode: bool = False
    dev_auto_user: Optional[str] = None


class DemoSettings(BaseModel):
    enabled: bool = True
    cell_map_path: str = "data/demo.json"


class RobotAppSettings(BaseModel):
    enabled: bool = False


class ScaleAppSettings(BaseModel):
    enabled: bool = False
