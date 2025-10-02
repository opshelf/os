#!/usr/bin/env python3
"""Generate OpenAPI documentation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from perpetual_os1_server.main import app
from fastapi.openapi.utils import get_openapi

def generate_openapi_json() -> dict:
    """Generate OpenAPI JSON specification."""
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    openapi_schema["tags"] = [
        {"name": "auth", "description": "Authentication and user management"},
        {"name": "robot", "description": "Robot control and configuration"},
        {"name": "gantry", "description": "Gantry movement and positioning"},
        {"name": "storage", "description": "Storage grid and inventory management"},
        {"name": "scanner", "description": "Barcode scanning operations"},
        {"name": "scale", "description": "Scale and weight measurements"},
        {"name": "printer", "description": "Label printing operations"},
        {"name": "sizes", "description": "File size management"},
        {"name": "users", "description": "User management"},
        {"name": "roles", "description": "Role and permission management"},
        {"name": "audit", "description": "Audit logging"}
    ]

    return openapi_schema

def main() -> None:
    """Generate and save OpenAPI specification."""
    openapi_schema = generate_openapi_json()

    docs_dir = Path(__file__).parent
    output_file = docs_dir / "openapi.json"

    with open(output_file, "w") as f:
        json.dump(openapi_schema, f, indent=2)

    print(f"OpenAPI specification generated: {output_file}")

if __name__ == "__main__":
    main()