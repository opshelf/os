#!/usr/bin/env python3
"""Compatibility entrypoint; prefer `uv run openapi`."""
from openshelf_os_server.openapi import generate_openapi_json, main

__all__ = ["generate_openapi_json", "main"]

if __name__ == "__main__":
    main()
