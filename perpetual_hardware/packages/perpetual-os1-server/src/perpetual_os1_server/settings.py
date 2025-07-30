from pydantic import BaseModel, Field
from typing import Optional
import secrets
import os
import json


class StorageSettings(BaseModel):
    filename: str = "data/storage.json"


class SizeDatabaseSettings(BaseModel):
    filename: str = "data/size.json"


class PrinterSettings(BaseModel):
    enabled: bool = False


class ScannerSettings(BaseModel):
    enabled: bool = False


class RobotSettings(BaseModel):
    enabled: bool = False


class CameraSettings(BaseModel):
    enabled: bool = False


class NatsSettings(BaseModel):
    domain: str = "localhost"
    port: int = 4222

    def connection_string(self) -> str:
        return f"nats://{self.domain}:{self.port}"


class AuthSettings(BaseModel):
    enabled: bool = True
    require_auth: bool = True
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    jwt_refresh_expire_days: int = 7
    dev_mode: bool = False
    dev_auto_user: Optional[str] = None


class Settings(BaseModel):
    storage: StorageSettings
    sizes: SizeDatabaseSettings
    printer: PrinterSettings
    scanner: ScannerSettings
    robot: RobotSettings
    nats: NatsSettings
    camera: CameraSettings
    auth: AuthSettings


def get_settings() -> Settings:
    settings_path = "data/settings.json"
    
    if os.path.exists(settings_path):
        with open(settings_path, "r") as f:
            data = json.loads(f.read())
    else:
        data = {}
    
    if "storage" not in data:
        data["storage"] = {"filename": "data/storage.json"}
    if "sizes" not in data:
        data["sizes"] = {"filename": "data/size.json"}
    if "printer" not in data:
        data["printer"] = {"enabled": False}
    if "scanner" not in data:
        data["scanner"] = {"enabled": False}
    if "robot" not in data:
        data["robot"] = {"enabled": False}
    if "nats" not in data:
        data["nats"] = {"domain": "localhost", "port": 4222}
    if "camera" not in data:
        data["camera"] = {"enabled": False}
    if "auth" not in data:
        data["auth"] = {}
    
    env = os.getenv("ENV", "").lower()
    if env in ["dev", "development"]:
        data["auth"]["dev_mode"] = True
        data["auth"]["require_auth"] = False
        data["auth"]["dev_auto_user"] = "admin@example.com"
    
    if os.getenv("JWT_SECRET"):
        data["auth"]["jwt_secret"] = os.getenv("JWT_SECRET")
    
    return Settings.model_validate(data)
