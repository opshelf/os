from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class OperatingMode(str, Enum):
    demo = "demo"
    development = "development"
    production = "production"


MODE_FILE = Path(os.getenv("OPENSHELF_DATA_DIR", "data")) / ".mode"


def get_operating_mode(mode_file: Path | None = None) -> OperatingMode:
    mode_path = mode_file if mode_file is not None else MODE_FILE

    mode_map = {
        "demo": OperatingMode.demo,
        "development": OperatingMode.development,
        "production": OperatingMode.production,
    }

    if mode_path.exists() is True:
        raw = mode_path.read_text(encoding="utf-8").strip().lower()
        if raw in mode_map:
            return mode_map[raw]

    raw = os.getenv("OPERATING_MODE", "").strip().lower()

    return mode_map.get(raw, OperatingMode.development)


def should_enable_sentry(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    return has_production_traits(current_mode)


def has_production_traits(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    return current_mode in {OperatingMode.demo, OperatingMode.production}


def should_enable_demo_features(mode: OperatingMode | None = None) -> bool:
    current_mode = mode if mode is not None else get_operating_mode()

    return current_mode == OperatingMode.demo
