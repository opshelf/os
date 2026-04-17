#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
	REPO_ROOT / 'perpetual_hardware/packages/perpetual_os1/src',
	REPO_ROOT / 'perpetual_hardware/packages/hardware/src',
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

import perpetual_os1.laser_angle_calibration as angle_module


def main() -> int:
	return angle_module.main()


if __name__ == '__main__':
	raise SystemExit(main())
