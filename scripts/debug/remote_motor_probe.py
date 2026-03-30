#!/usr/bin/env python3
"""
Motor status probe for remote opshelf devices.

Connects to a device via SSH relay, stops the API container to free the serial port,
then probes all configured motors via modbus and displays their status.
"""

import argparse
import subprocess
import sys
import signal
import os
import tempfile
import shutil
import atexit
import base64
from pathlib import Path

RELAY_HOST = "64.225.22.139"
REPO_ROOT = Path(__file__).resolve().parents[2]

TEMP_KEY = None


def resolve_team_key(explicit_path: str | None = None) -> Path:
    candidates: list[Path] = []

    if explicit_path is not None:
        candidates.append(Path(explicit_path).expanduser())

    env_path = os.environ.get("OPSHELF_TEAM_KEY")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    candidates.append(REPO_ROOT.parent / "provision" / "keys" / "team_access")

    for candidate in candidates:
        if candidate.exists() is True:
            return candidate.resolve()

    raise FileNotFoundError(
        "Could not find keys/team_access. Set --team-key or OPSHELF_TEAM_KEY, "
        "or keep the sibling provision repo checked out."
    )


def get_ssh_key(team_key: str | None = None) -> Path:
    global TEMP_KEY

    if TEMP_KEY is not None:
        return TEMP_KEY

    source_key = resolve_team_key(team_key)
    temp_dir = tempfile.mkdtemp(prefix="opshelf_")
    TEMP_KEY = Path(temp_dir) / "team_access"
    shutil.copy2(source_key, TEMP_KEY)
    os.chmod(TEMP_KEY, 0o600)

    def cleanup():
        if TEMP_KEY is not None and TEMP_KEY.parent.exists():
            shutil.rmtree(TEMP_KEY.parent, ignore_errors=True)

    atexit.register(cleanup)

    return TEMP_KEY


PROBE_SCRIPT = r'''
import time
import sys
import signal
import termios
import tty
import select

from pymodbus.client import ModbusSerialClient
from pymodbus.framer import FramerType

SERIAL_PORTS = ["/dev/ttyUSB0", "/dev/ttyUSB1"]
BAUDRATE = 115200

STATUS_REG = 0x1003
ALARM_REG = 0x2203
POSITION_REG_HIGH = 0x602C
POSITION_REG_LOW = 0x602D

running = True
old_settings = None

def signal_handler(sig, frame):
    global running
    running = False

def setup_terminal():
    global old_settings
    old_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())

def restore_terminal():
    if old_settings is not None:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

def key_pressed():
    return select.select([sys.stdin], [], [], 0)[0]

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

MOTORS = [
    {"id": 2, "name": "y_axis", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
    {"id": 3, "name": "stage", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
    {"id": 4, "name": "rotate", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
    {"id": 5, "name": "x_axis", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
    {"id": 9, "name": "gripper", "type": "servo", "status_reg": 0x07D0, "control_reg": 0x03E8},
    {"id": 30, "name": "z_axis", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
    {"id": 31, "name": "door", "type": "stepper", "status_reg": STATUS_REG, "position_reg_high": POSITION_REG_HIGH, "position_reg_low": POSITION_REG_LOW},
]

def load_motors():
    return MOTORS

def read_register(client, unit_id, address):
    try:
        result = client.read_holding_registers(address, count=1, device_id=unit_id)
        if result.isError():
            return None
        return result.registers[0]
    except Exception:
        return None

def probe_motor(client, motor):
    status_val = read_register(client, motor["id"], motor["status_reg"])
    alarm_val = read_register(client, motor["id"], ALARM_REG)

    if status_val is None:
        return {"status": "NO COMM", "alarm": None, "position": None}

    fault_bit = status_val & 0x01
    complete_bit = (status_val >> 5) & 0x01

    if motor["type"] == "servo":
        gact = status_val & 0x01
        gsta = (status_val >> 4) & 0x03
        gobj = (status_val >> 6) & 0x03
        if gact == 0:
            state = "INACTIVE"
        elif gsta != 0x03:
            state = "ACTIVATING"
        elif gobj == 0x00:
            state = "MOVING"
        else:
            state = "IDLE"
        position = None
    else:
        pos_high = read_register(client, motor["id"], motor["position_reg_high"])
        pos_low = read_register(client, motor["id"], motor["position_reg_low"])

        if pos_high is not None and pos_low is not None:
            position = (pos_high << 16) | pos_low
            if position > 0x7FFFFFFF:
                position -= 0x100000000
        else:
            position = None

        if fault_bit == 1:
            state = "FAULT"
        elif complete_bit == 1:
            state = "IDLE"
        else:
            state = "MOVING"

    return {"status": state, "alarm": alarm_val, "position": position}

def print_table(motors, results):
    print("\033[2J\033[H", end="")
    print(f"{'id':>2} {'name':<8} {'status':<8} {'alm':>3} {'position':>8}")
    print("-" * 36)

    for motor in motors:
        result = results.get(motor["id"], {})
        status = result.get("status", "?")
        alarm = result.get("alarm")
        position = result.get("position")

        alarm_str = str(alarm) if alarm is not None else "-"
        pos_str = str(position) if position is not None else "-"

        status_color = ""
        reset = "\033[0m"
        if status == "FAULT":
            status_color = "\033[91m"
        elif status == "NO COMM":
            status_color = "\033[93m"
        elif status == "IDLE":
            status_color = "\033[92m"
        elif status == "MOVING":
            status_color = "\033[94m"
        elif status == "ACTIVATING":
            status_color = "\033[96m"

        print(f"{motor['id']:>2} {motor['name']:<8} {status_color}{status:<8}{reset} {alarm_str:>3} {pos_str:>8}")

    print("-" * 36)
    print("press any key to exit")

def find_motor_port():
    for port in SERIAL_PORTS:
        client = ModbusSerialClient(
            port=port,
            baudrate=BAUDRATE,
            parity="N",
            stopbits=1,
            bytesize=8,
            timeout=0.5,
            framer=FramerType.RTU,
        )

        if not client.connect():
            continue

        try:
            result = client.read_holding_registers(0x1003, count=1, device_id=2)

            if hasattr(result, "registers") and len(result.registers) > 0:
                client.close()
                return port
        except Exception:
            pass

        client.close()

    return None

def probe_main():
    global running
    motors = load_motors()
    serial_port = find_motor_port()

    if serial_port is None:
        print("no motors found")
        sys.exit(1)

    client = ModbusSerialClient(
        port=serial_port,
        baudrate=BAUDRATE,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=1,
        framer=FramerType.RTU,
    )

    if not client.connect():
        print(f"failed to connect to {serial_port}")
        sys.exit(1)

    setup_terminal()

    try:
        while running:
            results = {}
            for motor in motors:
                results[motor["id"]] = probe_motor(client, motor)

            print_table(motors, results)

            for _ in range(20):
                if not running or key_pressed():
                    running = False
                    break
                time.sleep(0.1)
    finally:
        restore_terminal()
        client.close()

if __name__ == "__main__":
    probe_main()
'''


def calculate_port(device_id: str) -> int:
    hex_prefix = device_id[:4]
    return (int(hex_prefix, 16) % 10000) + 50000


def ssh_command(port: int, command: str) -> subprocess.CompletedProcess:
    key_path = get_ssh_key()

    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-i", str(key_path),
        "-p", str(port),
    ]

    cmd = ["ssh"] + ssh_opts + [f"opshelf@{RELAY_HOST}"]

    if command:
        cmd.append(command)

    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description="Probe motor status on remote opshelf device")
    parser.add_argument("device_id", help="Device ID (8-char hex)")
    parser.add_argument("--team-key", help="Path to keys/team_access used for the relay SSH connection")
    args = parser.parse_args()

    device_id = args.device_id
    port = calculate_port(device_id)
    key_path = get_ssh_key(args.team_key)

    print(f"connecting to device {device_id} on port {port}...")

    print("stopping api container...")
    result = ssh_command(port, "docker stop perpetual_os-api-1 2>/dev/null || true")

    if result.returncode != 0:
        print(f"warning: failed to stop container: {result.stderr}")

    container_stopped = True

    def restore_container(signum=None, frame=None):
        if container_stopped:
            print("\nrestoring api container...")
            ssh_command(port, "docker start perpetual_os-api-1 2>/dev/null || true")

        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGINT, restore_container)
    signal.signal(signal.SIGTERM, restore_container)

    try:
        print("uploading probe script...")

        script_b64 = base64.b64encode(PROBE_SCRIPT.encode()).decode()
        upload_cmd = f"echo '{script_b64}' | base64 -d > /tmp/motor_probe.py"
        result = ssh_command(port, upload_cmd)

        if result.returncode != 0:
            print(f"error: failed to upload script: {result.stderr}")
            return

        print("running motor probe...")

        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-i", str(key_path),
            "-p", str(port),
            "-t", "-t",
        ]

        cmd = ["ssh"] + ssh_opts + [f"opshelf@{RELAY_HOST}", "python3 /tmp/motor_probe.py"]

        subprocess.run(cmd)

    finally:
        restore_container()


if __name__ == "__main__":
    main()
