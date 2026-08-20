#!/usr/bin/env python3
"""Supervised API-side Y first-home timing diagnostic for lb4a-1.

This client never starts/restarts the application, changes configuration, or
acknowledges safety. For a machine explicitly confirmed to have no cabinet doors,
it can enable only that component bypass after a strict live-state gate. It then
uses a recent stopped-machine position trace and requests the existing single-axis
Y home endpoint.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "http://127.0.0.1:8000"
HTTP_TIMEOUT_SECONDS = 10.0
HOME_TIMEOUT_SECONDS = 180.0
READY_WAIT_SECONDS = 180.0
READY_POLL_SECONDS = 0.5
MAX_POSITION_TRACE_AGE_SECONDS = 30 * 60
Y_MIN_MM = -1.0
Y_MAX_MM = 50.0
MOTION_RUNNING_BIT = 2
HOME_CONFIRMATION = "SUPERVISED-Y-HOME-RECENTLY-VERIFIED-UNDER-50MM"
CABINET_BYPASS_NAME = "cabinet_doors"
HOME_SETTLE_SECONDS = {
    "home-immediate": 0.0,
    "home-after-5s": 5.0,
}


def emit(event: str, **fields: Any) -> None:
    print(
        json.dumps({"timestamp": time.time(), "event": event, **fields}, sort_keys=True),
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a development API started with home_on_init=false and optionally "
            "request one supervised Y-axis home."
        )
    )
    parser.add_argument("--email", default="developer@opshelf.com")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Read-only API/configuration preflight.")
    for command, settle_seconds in HOME_SETTLE_SECONDS.items():
        home = subparsers.add_parser(
            command,
            help=f"Request one Y home after a fixed {settle_seconds:g} second settle.",
        )
        home.add_argument("--position-trace", type=Path, required=True)
        home.add_argument("--confirm", required=True)
        home.add_argument(
            "--enable-absent-cabinet-doors-bypass",
            action="store_true",
            help=(
                "After proving cabinet_doors is the only active safety source, enable "
                "only that component bypass. Use only where doors are not installed."
            ),
        )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command in HOME_SETTLE_SECONDS and args.confirm != HOME_CONFIRMATION:
        raise ValueError(
            "home confirmation mismatch; this command requires onsite E-stop "
            "supervision and a recent stopped-machine Y position trace"
        )


def request_json(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(API_BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"API {method} {path} returned HTTP {exc.code}: {raw}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"API {method} {path} failed: {exc}") from exc
    if status < 200 or status >= 300:
        raise RuntimeError(f"API {method} {path} returned HTTP {status}: {raw}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API {method} {path} returned non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"API {method} {path} returned a non-object JSON value")
    return parsed


def request_robot_status(token: str) -> dict[str, Any]:
    request = Request(
        API_BASE + "/api/robot/status",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError(f"robot status stream returned HTTP {response.status}")
            for _ in range(512):
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line.removeprefix("data:").strip())
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                robot_status = payload.get("robot_status")
                if not isinstance(robot_status, dict):
                    continue
                safety = robot_status.get("safety")
                y_axis = (robot_status.get("gantry") or {}).get("y_axis")
                if isinstance(safety, dict) and isinstance(y_axis, dict):
                    return payload
    except HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"robot status stream returned HTTP {exc.code}: {raw}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"robot status stream failed: {exc}") from exc
    raise RuntimeError("robot status stream did not provide safety and Y-axis state")


def get_pin() -> str:
    pin = os.getenv("OPENSHELF_DIAGNOSTIC_PIN")
    if pin is None and sys.stdin.isatty():
        pin = getpass.getpass("Developer PIN: ")
    if not pin:
        raise RuntimeError(
            "set OPENSHELF_DIAGNOSTIC_PIN or run interactively so the PIN can be prompted"
        )
    return pin


def authenticate(email: str) -> str:
    response = request_json(
        "POST",
        "/api/auth/login/json",
        payload={"email": email, "pin": get_pin()},
    )
    token = response.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("login response did not contain an access token")
    return token


def api_preflight(
    token: str, *, allow_cabinet_doors_bypass: bool = False
) -> dict[str, Any]:
    health = request_json("GET", "/api/health")
    expected_health = {
        "status": "ok",
        "mode": "development",
        "robot_status": "ok",
        "robot_phase": "ready",
    }
    mismatches = {
        field: {"expected": expected, "actual": health.get(field)}
        for field, expected in expected_health.items()
        if health.get(field) != expected
    }
    if mismatches:
        raise RuntimeError(
            "API is not ready for the first-home diagnostic: "
            + json.dumps(mismatches, sort_keys=True)
        )

    home_setting = request_json("GET", "/api/system/home-on-init", token=token)
    if home_setting.get("home_on_init") is not False:
        raise RuntimeError("home_on_init must already be false; refusing to change it")

    motors = request_json("GET", "/api/robot/debug/motors", token=token)
    tool_motors = motors.get("tool")
    if not isinstance(tool_motors, list) or not any(
        isinstance(motor, dict)
        and motor.get("name") == "y_axis"
        and motor.get("motor_id") == 2
        for motor in tool_motors
    ):
        raise RuntimeError("API does not report the confirmed y_axis motor at Modbus unit 2")

    status_payload = request_robot_status(token)
    robot_status = status_payload["robot_status"]
    safety = robot_status["safety"]
    component_bypasses = safety.get("component_bypasses")
    expected_component_bypasses = (
        {CABINET_BYPASS_NAME} if allow_cabinet_doors_bypass else set()
    )
    enabled_component_bypasses = (
        {
            name
            for name, enabled in component_bypasses.items()
            if enabled is True
        }
        if isinstance(component_bypasses, dict)
        else set()
    )
    invalid_component_bypass_value = isinstance(component_bypasses, dict) and any(
        type(enabled) is not bool for enabled in component_bypasses.values()
    )
    unsafe_safety = (
        safety.get("active") is not False
        or safety.get("motion_blocked") is not False
        or safety.get("awaiting_ack") is not False
        or safety.get("bypassed") is not False
        or safety.get("active_sources") != []
        or not isinstance(component_bypasses, dict)
        or invalid_component_bypass_value
        or enabled_component_bypasses != expected_component_bypasses
    )
    if unsafe_safety:
        raise RuntimeError(
            "safety guard must be clear and acknowledged, with exactly the explicitly "
            "allowed component bypasses: "
            + json.dumps(safety, sort_keys=True)
        )
    if (
        status_payload.get("current_command") is not None
        or status_payload.get("queue_length") != 0
        or status_payload.get("is_crashed") is not False
        or status_payload.get("is_emergency_stop_active") is not False
    ):
        raise RuntimeError("robot status reports a command, queue, crash, or active E-stop")

    y_axis = robot_status["gantry"]["y_axis"]
    y_position = y_axis.get("position")
    if not isinstance(y_position, (int, float)) or not math.isfinite(float(y_position)):
        raise RuntimeError("live API status has no finite Y position")
    if not Y_MIN_MM <= float(y_position) <= Y_MAX_MM:
        raise RuntimeError(
            f"live API places Y outside {Y_MIN_MM:g}..{Y_MAX_MM:g} mm: {y_position}"
        )

    emit(
        "api_preflight_complete",
        health=health,
        home_on_init=False,
        y_unit=2,
        live_y_position_mm=float(y_position),
        safety=safety,
        allowed_component_bypasses=sorted(expected_component_bypasses),
    )
    return health


def enable_absent_cabinet_doors_bypass(token: str) -> None:
    """Enable only the physically absent cabinet-door input after a strict live gate."""
    home_setting = request_json("GET", "/api/system/home-on-init", token=token)
    if home_setting.get("home_on_init") is not False:
        raise RuntimeError("home_on_init must be false before enabling the door bypass")
    motors = request_json("GET", "/api/robot/debug/motors", token=token)
    tool_motors = motors.get("tool")
    if not isinstance(tool_motors, list) or not any(
        isinstance(motor, dict)
        and motor.get("name") == "y_axis"
        and motor.get("motor_id") == 2
        for motor in tool_motors
    ):
        raise RuntimeError("confirmed y_axis at Modbus unit 2 is not present")

    status_payload = request_robot_status(token)
    robot_status = status_payload["robot_status"]
    safety = robot_status["safety"]
    component_bypasses = safety.get("component_bypasses")
    sources = safety.get("sources")
    cabinet_source = sources.get(CABINET_BYPASS_NAME) if isinstance(sources, dict) else None
    curtain_source = sources.get("light_curtain") if isinstance(sources, dict) else None
    y_axis = robot_status.get("gantry", {}).get("y_axis", {})
    y_position = y_axis.get("position")

    unsafe = (
        status_payload.get("current_command") is not None
        or status_payload.get("queue_length") != 0
        or status_payload.get("is_crashed") is not False
        or status_payload.get("is_emergency_stop_active") is not False
        or safety.get("active") is not True
        or safety.get("motion_blocked") is not True
        or safety.get("bypassed") is not False
        or safety.get("active_sources") != [CABINET_BYPASS_NAME]
        or not isinstance(component_bypasses, dict)
        or any(value is not False for value in component_bypasses.values())
        or not isinstance(cabinet_source, dict)
        or cabinet_source.get("active") is not True
        or cabinet_source.get("open") is not True
        or cabinet_source.get("bypassed") is not False
        or not isinstance(curtain_source, dict)
        or curtain_source.get("active") is not False
        or curtain_source.get("bypassed") is not False
        or not isinstance(y_position, (int, float))
        or not math.isfinite(float(y_position))
        or not Y_MIN_MM <= float(y_position) <= Y_MAX_MM
    )
    if unsafe:
        raise RuntimeError(
            "refusing cabinet-door bypass: live state is not the exact expected "
            "doorless-machine condition: " + json.dumps(status_payload, sort_keys=True)
        )

    emit(
        "cabinet_doors_bypass_request_imminent",
        reason="cabinet doors and sensors are not installed on this machine",
        live_y_position_mm=float(y_position),
        safety=safety,
    )
    response = request_json(
        "POST",
        "/api/robot/safety/override/component",
        token=token,
        payload={"component": CABINET_BYPASS_NAME, "bypass": True},
    )
    response_components = response.get("component_bypasses")
    enabled_components = (
        {name for name, enabled in response_components.items() if enabled is True}
        if isinstance(response_components, dict)
        else set()
    )
    if (
        response.get("active") is not False
        or response.get("motion_blocked") is not False
        or response.get("awaiting_ack") is not False
        or response.get("bypassed") is not False
        or response.get("active_sources") != []
        or enabled_components != {CABINET_BYPASS_NAME}
        or not isinstance(response_components, dict)
        or any(type(value) is not bool for value in response_components.values())
    ):
        raise RuntimeError(
            "cabinet-door bypass response was not the exact safe scoped state: "
            + json.dumps(response, sort_keys=True)
        )
    emit("cabinet_doors_bypass_enabled", safety=response)


def wait_for_ready_api() -> None:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    last_error = "API has not responded"
    while True:
        try:
            health = request_json("GET", "/api/health")
            if (
                health.get("status") == "ok"
                and health.get("mode") == "development"
                and health.get("robot_status") == "ok"
                and health.get("robot_phase") == "ready"
            ):
                emit("api_ready_observed", health=health)
                return
            last_error = json.dumps(health, sort_keys=True)
        except RuntimeError as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"API did not reach development/ready within {READY_WAIT_SECONDS:g}s: "
                f"{last_error}"
            )
        time.sleep(min(READY_POLL_SECONDS, remaining))


def load_recent_position_evidence(path: Path) -> dict[str, Any]:
    try:
        age_seconds = time.time() - path.stat().st_mtime
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read position trace {path}: {exc}") from exc
    if age_seconds < -5 or age_seconds > MAX_POSITION_TRACE_AGE_SECONDS:
        raise RuntimeError(
            f"position trace must be no older than {MAX_POSITION_TRACE_AGE_SECONDS // 60} "
            f"minutes; age={age_seconds:.1f}s"
        )

    snapshots: list[dict[str, Any]] = []
    last_event_name: str | None = None
    completed = False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_name = event.get("event")
        if isinstance(event_name, str):
            last_event_name = event_name
        if event_name == "inspection_complete":
            completed = event.get("writes") == 0
        snapshot = event.get("snapshot")
        if event_name in {"inspection_preflight", "inspection_sample"} and isinstance(
            snapshot, dict
        ):
            snapshots.append(snapshot)
    if not completed or last_event_name != "inspection_complete" or not snapshots:
        raise RuntimeError("position trace is not a completed read-only timing inspection")

    snapshot = snapshots[-1]
    position = snapshot.get("position_mm")
    status = snapshot.get("status")
    alarm = snapshot.get("alarm")
    so2 = snapshot.get("so2_function")
    if not isinstance(position, (int, float)) or not math.isfinite(float(position)):
        raise RuntimeError("position trace has no finite Y position")
    if not Y_MIN_MM <= float(position) <= Y_MAX_MM:
        raise RuntimeError(
            f"position trace places Y outside {Y_MIN_MM:g}..{Y_MAX_MM:g} mm: {position}"
        )
    if type(status) is not int or status & (1 << MOTION_RUNNING_BIT):
        raise RuntimeError(f"position trace does not show stationary Y: status={status!r}")
    if alarm != 0:
        raise RuntimeError(f"position trace shows Y alarm: {alarm!r}")
    if so2 != 0x24:
        raise RuntimeError(f"position trace does not show dedicated BRK SO2 0x24: {so2!r}")

    evidence = {
        "path": str(path),
        "age_seconds": age_seconds,
        "position_mm": float(position),
        "status": status,
        "alarm": alarm,
        "so2_function": so2,
    }
    emit("position_evidence_accepted", **evidence)
    return evidence


def countdown(command: str, settle_seconds: float) -> None:
    emit(
        "home_armed",
        command=command,
        settle_seconds=settle_seconds,
        requirement=(
            "onsite engineer at E-stop; load restrained; abort on harsh noise, "
            "vibration, brake chatter, drop, or unexpected motion"
        ),
    )
    deadline = time.monotonic() + settle_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        emit("settle_countdown", command=command, remaining_s=round(remaining, 3))
        time.sleep(min(1.0, remaining))


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    if args.command == "check":
        token = authenticate(args.email)
        api_preflight(token)
        emit("check_complete", writes=0, motion_requests=0)
        return 0

    evidence = load_recent_position_evidence(args.position_trace)
    wait_for_ready_api()
    token = authenticate(args.email)
    enable_cabinet_doors_bypass = getattr(
        args, "enable_absent_cabinet_doors_bypass", False
    )
    if enable_cabinet_doors_bypass:
        enable_absent_cabinet_doors_bypass(token)
    allow_cabinet_doors_bypass = enable_cabinet_doors_bypass
    api_preflight(
        token, allow_cabinet_doors_bypass=allow_cabinet_doors_bypass
    )
    settle_seconds = HOME_SETTLE_SECONDS[args.command]
    countdown(args.command, settle_seconds)
    # Repeat the software gate immediately before the only motion request.
    api_preflight(
        token, allow_cabinet_doors_bypass=allow_cabinet_doors_bypass
    )
    emit(
        "y_home_request_imminent",
        endpoint="/api/robot/debug/motors/y_axis/home",
        position_evidence=evidence,
    )
    started = time.monotonic()
    response = request_json(
        "POST",
        "/api/robot/debug/motors/y_axis/home",
        token=token,
        timeout=HOME_TIMEOUT_SECONDS,
    )
    elapsed = time.monotonic() - started
    if response != {"success": True, "motor": "y_axis"}:
        raise RuntimeError(f"unexpected Y home response: {response!r}")
    emit("y_home_complete", elapsed_seconds=elapsed, response=response)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        emit("interrupted")
        return 130
    except Exception as exc:
        emit("fatal", error=str(exc), error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
