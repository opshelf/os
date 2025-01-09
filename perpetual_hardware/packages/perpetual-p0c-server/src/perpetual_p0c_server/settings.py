from pydantic import BaseModel


class StorageSettings(BaseModel):
    filename: str = "storage.json"


class SizeDatabaseSettings(BaseModel):
    filename: str = "size.json"

class PrinterSettings(BaseModel):
    enabled: bool = False

class ScannerSettings(BaseModel):
    enabled: bool = False

class RobotSettings(BaseModel):
    enabled: bool = False

class Settings(BaseModel):
    storage: StorageSettings
    sizes: SizeDatabaseSettings
    printer: PrinterSettings
    scanner: ScannerSettings
    robot: RobotSettings


def get_settings() -> Settings:
    with open("settings.json", "r") as f:
        return Settings.model_validate_json(f.read())
