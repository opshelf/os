from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class OperatingMode(str, Enum):
    demo = "demo"
    development = "development"
    mock = "mock"
    production = "production"


MODE_FILE = Path(os.getenv("OPENSHELF_DATA_DIR", "data")) / ".mode"


def get_operating_mode(mode_file: Path | None = None) -> OperatingMode:
    mode_path = mode_file if mode_file is not None else MODE_FILE

    mode_map = {
        "demo": OperatingMode.demo,
        "development": OperatingMode.development,
        "mock": OperatingMode.mock,
        "production": OperatingMode.production,
    }

    if mode_path.exists() is True:
        raw = mode_path.read_text(encoding="utf-8").strip().lower()
        if raw in mode_map:
            return mode_map[raw]

    raw = os.getenv("OPERATING_MODE", "").strip().lower()

    return mode_map.get(raw, OperatingMode.development)


# Set by the api image entrypoint for the service it starts, and nowhere else.
SERVICE_PROCESS_ENV = "OPSHELF_SERVICE_PROCESS"


def is_service_process() -> bool:
    """Whether this process is the device's long-running service.

    `docker exec`, `docker compose run --entrypoint ...` and ad-hoc `python -c` share the
    service's environment, DSNs included, but not its entrypoint, so only the entrypoint
    marks the process. Their errors are the operator's to read, not device faults.
    """
    return os.getenv(SERVICE_PROCESS_ENV, "") == "1"


def should_enable_sentry(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    # Demo devices run real hardware but their errors are not customer incidents;
    # keep them in local logs. Mock devices do not run hardware at all.
    return current_mode == OperatingMode.production


def has_production_traits(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    return current_mode in {OperatingMode.demo, OperatingMode.mock, OperatingMode.production}


def should_enable_demo_features(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    return current_mode == OperatingMode.demo
