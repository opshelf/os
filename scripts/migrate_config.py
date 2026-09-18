#!/usr/bin/env python3
"""Run with the backend environment: uv run --no-sync python ../scripts/migrate_config.py data."""

import os
from pathlib import Path
import sys


if __name__ == "__main__":
    # This is an offline config operation; disable the package's import-time telemetry.
    os.environ["SENTRY_DSN_HARDWARE"] = ""
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "openshelf_hardware/packages/openshelf_os/src")
    )
    from openshelf_os.config_migrations.cli import main

    raise SystemExit(main())
