#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_CANDIDATES = [
	Path("data/config.json"),
	REPO_ROOT / "data/config.json",
	REPO_ROOT / "perpetual_hardware/data/config.json",
]


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Print motor, scale, and laser serial topology from config and host devices.",
	)
	parser.add_argument("--config")
	parser.add_argument("--show-lsof", action="store_true")
	return parser.parse_args()


def resolve_config_path(raw_path: str | None) -> Path:
	if raw_path is not None:
		path = Path(raw_path)
		if path.exists() is True:
			return path

		raise FileNotFoundError(f"Config file not found: {raw_path}")

	for candidate in DEFAULT_CONFIG_CANDIDATES:
		if candidate.exists() is True:
			return candidate

	raise FileNotFoundError(
		"Config file not found. Pass --config and point it to the machine's data/config.json"
	)


def load_config(path: Path) -> dict[str, Any]:
	return json.loads(path.read_text())


def build_bus_settings(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
	defaults = {
		"motor": {
			"ports": ["/dev/ttyUSB1"],
			"baudrate": 115200,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"scale": {
			"ports": ["/dev/ttyUSB0"],
			"baudrate": 9600,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"laser": {
			"ports": ["/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B001K430-if00-port0"],
			"baudrate": 9600,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
	}

	robot_cfg = config.get("robot", {})
	bus_cfg = robot_cfg.get("bus", {})
	settings: dict[str, dict[str, Any]] = {}

	for name in ("motor", "scale", "laser"):
		current = defaults[name].copy()
		cfg = bus_cfg.get(name)

		if cfg is not None:
			current.update(
				{
					"ports": list(cfg.get("ports", current["ports"])),
					"baudrate": cfg.get("baudrate", current["baudrate"]),
					"parity": cfg.get("parity", current["parity"]),
					"timeout": cfg.get("timeout", current["timeout"]),
					"retry_delay_s": cfg.get("retry_delay_s", current["retry_delay_s"]),
				}
			)

		settings[name] = current

	return settings


def run_command(args: list[str]) -> tuple[int, str]:
	try:
		result = subprocess.run(
			args,
			check=False,
			capture_output=True,
			text=True,
		)
	except FileNotFoundError:
		return 127, ""

	output = result.stdout.strip()
	if output == "" and result.stderr.strip() != "":
		output = result.stderr.strip()

	return result.returncode, output


def resolve_device(path_str: str) -> dict[str, Any]:
	path = Path(path_str)
	exists = path.exists()
	resolved = os.path.realpath(path_str) if exists is True else None
	is_symlink = path.is_symlink() if exists is True else False

	udev_path = None
	udev_serial = None

	if exists is True:
		code, output = run_command(["udevadm", "info", "-q", "path", "-n", path_str])
		if code == 0 and output != "":
			udev_path = output

		code, output = run_command(["udevadm", "info", "--query=property", "--name", path_str])
		if code == 0 and output != "":
			for line in output.splitlines():
				if line.startswith("ID_SERIAL="):
					udev_serial = line.split("=", 1)[1]
					break

	return {
		"path": path_str,
		"exists": exists,
		"is_symlink": is_symlink,
		"resolved": resolved,
		"udev_path": udev_path,
		"udev_serial": udev_serial,
	}


def print_bus_report(name: str, settings: dict[str, Any]) -> None:
	print(f"{name}:")
	print(f"\tbaudrate: {settings['baudrate']}")
	print(f"\tparity: {settings['parity']}")
	print(f"\ttimeout: {settings['timeout']}")
	print(f"\tports: {', '.join(settings['ports'])}")

	for port in settings["ports"]:
		info = resolve_device(port)
		print(f"\tdevice: {port}")
		print(f"\t\texists: {info['exists']}")

		if info["resolved"] is not None:
			print(f"\t\tresolved: {info['resolved']}")

		if info["is_symlink"] is True:
			print(f"\t\tsymlink: true")

		if info["udev_serial"] is not None:
			print(f"\t\tid_serial: {info['udev_serial']}")

		if info["udev_path"] is not None:
			print(f"\t\tudev_path: {info['udev_path']}")


def print_collision_report(settings: dict[str, dict[str, Any]]) -> None:
	resolved_by_bus: dict[str, str] = {}

	for name, bus in settings.items():
		ports = bus["ports"]
		if len(ports) == 0:
			continue

		resolved = os.path.realpath(ports[0]) if Path(ports[0]).exists() is True else ports[0]
		resolved_by_bus[name] = resolved

	print("host_port_comparison:")

	names = ("motor", "scale", "laser")
	for idx, left in enumerate(names):
		for right in names[idx + 1:]:
			left_port = resolved_by_bus.get(left)
			right_port = resolved_by_bus.get(right)

			if left_port is None or right_port is None:
				status = "unknown"
			elif left_port == right_port:
				status = "same"
			else:
				status = "different"

			print(f"\t{left}_vs_{right}: {status}")


def print_glob(title: str, pattern: str) -> None:
	print(f"{title}:")
	paths = sorted(str(path) for path in Path("/").glob(pattern))

	if len(paths) == 0:
		print("\tnone")
		return

	for path in paths:
		resolved = os.path.realpath(path)
		if resolved != path:
			print(f"\t{path} -> {resolved}")
			continue

		print(f"\t{path}")


def print_command_output(title: str, args: list[str]) -> None:
	print(f"{title}:")
	code, output = run_command(args)

	if code == 127:
		print("\tcommand not found")
		return

	if output == "":
		print(f"\tno output (exit={code})")
		return

	for line in output.splitlines():
		print(f"\t{line}")


def main() -> int:
	args = parse_args()

	try:
		config_path = resolve_config_path(args.config)
		config = load_config(config_path)
	except FileNotFoundError as exc:
		print(str(exc), file=sys.stderr)
		return 1

	settings = build_bus_settings(config)

	print(f"config: {config_path}")
	print()
	print("configured_buses:")

	for name in ("motor", "scale", "laser"):
		print_bus_report(name, settings[name])

	print()
	print_collision_report(settings)
	print()
	print_glob("serial_by_id", "dev/serial/by-id/*")
	print()
	print_glob("ttyusb", "dev/ttyUSB*")
	print()
	print_glob("ttyacm", "dev/ttyACM*")
	print()
	print_command_output("lsusb_tree", ["lsusb", "-t"])
	print()
	print_command_output("dmesg_serial", ["sh", "-lc", "dmesg | rg 'ttyUSB|ttyACM|FTDI|CP210|CH340|PL2303' -i | tail -n 80"])

	if args.show_lsof is True:
		print()
		print_command_output(
			"lsof_serial",
			["sh", "-lc", "lsof /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* 2>/dev/null || true"],
		)

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
