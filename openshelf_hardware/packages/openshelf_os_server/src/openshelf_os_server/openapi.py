from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from fastapi.openapi.utils import get_openapi

from openshelf_os_server.main import app


OPENAPI_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


def build_openapi_document() -> dict:
	"""Build the schema from the same mounted routes served by FastAPI."""
	production_routes = [
		route
		for route in app.routes
		if not str(getattr(route, "path", "")).startswith("/api/test/")
	]
	return get_openapi(
		title=app.title,
		version=app.version,
		description=app.description,
		routes=production_routes,
		tags=app.openapi_tags,
	)


def render_openapi_document() -> str:
	return json.dumps(build_openapi_document(), indent=2, ensure_ascii=False) + "\n"


def write_openapi_document(path: Path = OPENAPI_PATH) -> None:
	path.write_text(render_openapi_document(), encoding="utf-8")


def openapi_document_is_current(path: Path = OPENAPI_PATH) -> bool:
	if path.exists() is False:
		return False
	return path.read_text(encoding="utf-8") == render_openapi_document()


def main(argv: Sequence[str] | None = None) -> None:
	parser = argparse.ArgumentParser(description="Generate or verify the OpenShelf OpenAPI snapshot")
	parser.add_argument(
		"--check",
		action="store_true",
		help="exit non-zero when docs/openapi.json differs from the mounted API",
	)
	args = parser.parse_args(argv)

	if args.check:
		if openapi_document_is_current() is False:
			raise SystemExit(
				"OpenAPI snapshot is stale; run `uv run openapi` from openshelf_hardware"
			)
		print(f"OpenAPI snapshot is current: {OPENAPI_PATH}")
		return

	write_openapi_document()
	print(f"OpenAPI snapshot generated: {OPENAPI_PATH}")


if __name__ == "__main__":
	main()
