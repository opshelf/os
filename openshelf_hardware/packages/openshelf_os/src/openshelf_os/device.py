from __future__ import annotations

import os
import socket
from pathlib import Path

DEVICE_ID_PATH = Path("/etc/opshelf/device-id")
DEVICE_ALIAS_PATH = Path("/etc/opshelf/device-alias")
RELEASE_PATH = Path("/etc/opshelf/release")

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


def _release_lines() -> list[str]:
	try:
		lines = RELEASE_PATH.read_text(encoding="utf-8").splitlines()
	except OSError:
		return []
	return [line.strip() for line in lines]


def get_release_sha() -> str:
	# The sync scripts stage the running release in RELEASE_PATH on every sync;
	# the environment wins for development stacks that build from a checkout.
	release_sha = os.getenv("RELEASE_SHA", "").strip()

	if release_sha != "":
		return release_sha.lower()

	lines = _release_lines()
	return lines[0].lower() if len(lines) > 0 and lines[0] != "" else ""


def get_release_branch() -> str:
	release_branch = os.getenv("RELEASE_BRANCH", "").strip()

	if release_branch != "":
		return release_branch.lower()

	lines = _release_lines()
	return lines[1].lower() if len(lines) > 1 and lines[1] != "" else ""
