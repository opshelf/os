from __future__ import annotations

import os
from pathlib import Path

from perpetual_os1.common import OperatingMode

MODE_FILE = Path("data/.mode")


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
	return current_mode in {OperatingMode.demo, OperatingMode.production}
