from __future__ import annotations

import os
import socket
from pathlib import Path

DEVICE_ID_PATH = Path("/etc/opshelf/device-id")
DEVICE_ALIAS_PATH = Path("/etc/opshelf/device-alias")

def get_device_id() -> str:
	device_id = os.getenv("DEVICE_ID", "").strip().lower()

	if device_id != "":
		return device_id

	try:
		device_id = DEVICE_ID_PATH.read_text(encoding="utf-8").strip().lower()
	except OSError:
		device_id = ""

	if device_id != "":
		return device_id

	hostname = socket.gethostname().strip().lower()

	if hostname != "":
		return hostname

	return "unknown"


def get_device_alias() -> str:
	try:
		return DEVICE_ALIAS_PATH.read_text(encoding="utf-8").strip().lower()
	except OSError:
		return ""
