from pydantic import BaseModel


class StorageSettings(BaseModel):
    filename: str = "storage.json"


class SizeDatabaseSettings(BaseModel):
    filename: str = "size.json"


class Settings(BaseModel):
    storage: StorageSettings
    sizes: SizeDatabaseSettings


def get_settings() -> Settings:
    with open("settings.json", "r") as f:
        return Settings.model_validate_json(f.read())
