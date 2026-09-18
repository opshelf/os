"""Persisted config schema versions are independent of the application's version."""

from typing import Any

CURRENT_SCHEMA_VERSION = 4
MIGRATION_COMMAND = (
    "cd openshelf_hardware && uv run --no-sync python ../scripts/migrate_config.py data --write"
)


def read_schema_version(config: dict[str, Any]) -> int:
    version = config.get("schema_version", 1)
    if type(version) is not int or version < 1:
        raise ValueError("schema_version must be a positive integer (absent means version 1)")
    return version


def require_current_schema(config: dict[str, Any]) -> None:
    version = read_schema_version(config)
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Config schema {version} is newer than supported schema {CURRENT_SCHEMA_VERSION}; "
            "upgrade firmware or restore a matching config backup"
        )
    if version < CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Config schema {version} requires migration to {CURRENT_SCHEMA_VERSION}. "
            f"Stop the application, then run from the checkout: {MIGRATION_COMMAND}"
        )


def reject_legacy_keys(value: Any, renames: dict[str, str], removed: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        legacy = [key for key in (*renames, *removed) if key in value]
        if legacy:
            raise ValueError(
                "Legacy laser scan config keys are not supported: "
                + ", ".join(legacy)
                + ". Migrate the original config before setting schema_version."
            )
    return value
