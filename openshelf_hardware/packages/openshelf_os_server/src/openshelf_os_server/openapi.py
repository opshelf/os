"""Export exactly the schema served by the application, without running its lifespan."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def generate_openapi_json() -> dict:
    # Set import-time paths before importing the application. No production settings,
    # hardware startup, migrations, or credentials are needed for route introspection.
    with tempfile.TemporaryDirectory(prefix="opshelf-openapi-") as directory:
        environment = {
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "SENTRY_DSN_API": "",
            "OPENSHELF_DATA_DIR": directory,
            "OPENSHELF_CONFIG_PATH": str(Path(directory) / "config.json"),
            "OPENSHELF_ONBOARDING_STATE_PATH": str(Path(directory) / "onboarding_state"),
        }
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            from openshelf_os_server.main import app  # noqa: PLC0415 - isolate environment first

            return app.openapi()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[2] / "docs/openapi.json"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the generated document through the contract checks; do not write",
    )
    args = parser.parse_args()
    rendered = json.dumps(generate_openapi_json(), indent=2, sort_keys=True) + "\n"
    if args.check:
        from openshelf_os_server.openapi_checks import validate_document  # noqa: PLC0415

        validate_document(json.loads(rendered))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
