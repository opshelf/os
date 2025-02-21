from pydantic import BaseModel


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


class NatsSettings(BaseModel):
    domain: str = "localhost"
    port: int = 4222

    def connection_string(self) -> str:
        return f"nats://{self.domain}:{self.port}"


class Settings(BaseModel):
    storage: StorageSettings
    sizes: SizeDatabaseSettings
    printer: PrinterSettings
    scanner: ScannerSettings
    robot: RobotSettings
    nats: NatsSettings


def get_settings() -> Settings:
    with open("data/settings.json", "r") as f:
        return Settings.model_validate_json(f.read())
