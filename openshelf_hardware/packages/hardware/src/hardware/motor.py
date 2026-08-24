from __future__ import annotations

import time
import asyncio
import functools
import logging
import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Callable, Awaitable

from hardware import motor_helpers
from hardware.controller import SharedModbusController

if TYPE_CHECKING:
    from hardware.curtain import MotionSafetyInterlock

from hardware.exceptions import (
    MotorError, MotorTimeoutError, MotorLimitError,
    MotorConfigError
)

MOTOR_MOVE_TIMEOUT = 180.0
MOTOR_HOME_TIMEOUT = 180.0
MOTOR_JOG_TIMEOUT = 10.0
MOTOR_WAIT_TIMEOUT = 60.0
POSITION_TOLERANCE_MM = 1.0
NOOP_POSITION_TOLERANCE_MM = 0.01
HOME_APPROACH_TOLERANCE_MM = 1.0
DEFAULT_HOME_APPROACH_BACKOFF_MM = 5.0
BRAKE_SIGNAL_OUTPUT_FUNCTIONS = frozenset({0x24, 0xA4, 0xA1})
DEFAULT_BRAKE_RELEASE_DELAY_MS = 250
DEFAULT_BRAKE_ENGAGEMENT_DELAY_MS = 250
DEFAULT_BRAKE_ENGAGEMENT_SPEED_THRESHOLD = 10
DEFAULT_STALL_TIMEOUT_S = 5.0
SOFTWARE_STALL_CONFIRM_DELAY_S = 0.05
Y_AXIS_RUNTIME_PROFILE = {
    "Ip": 60,
    "control_mode": 2,
    "ppr": 10000,
    "encoder_resolution_cpr": 4000,
    "brake_output_function": 0xA1,
    "brake_release_delay_ms": 250,
    "brake_engagement_delay_ms": 250,
    "brake_engagement_speed_threshold": 10,
}

logger = logging.getLogger(__name__)


class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3
    FAULT = 4
    TIMEOUT = 5


TERMINAL_MOTOR_STATUSES = frozenset({
    MotorStatus.ERROR,
    MotorStatus.FAULT,
    MotorStatus.TIMEOUT,
})


@dataclass
class MotionWaitState:
    start_time: float
    last_movement_time: float
    last_pos: Optional[float] = None
    has_moved: bool = False

    def reset(self, now: float) -> None:
        self.start_time = now
        self.last_movement_time = now
        self.last_pos = None
        self.has_moved = False


@dataclass(frozen=True)
class MotionSnapshot:
    status_word: int
    is_complete: bool
    has_fault_bit: bool


@dataclass(frozen=True)
class MotionAlarmState:
    alarm_code: Optional[int]
    is_active: bool


@dataclass(frozen=True)
class MotionFaultState:
    is_fault: bool
    stall_detected: bool
    no_progress: bool


@dataclass
class MotionRecordEntry:
    timestamp: str
    elapsed_s: float
    motor_id: int
    motor_name: str | None
    status_word: int | None
    is_complete: bool | None
    has_fault_bit: bool | None
    alarm_code: int | None
    alarm_active: bool | None
    raw_position: int | None
    logical_position_mm: float | None
    position_read_latency_s: float | None
    position_delta_mm: float | None
    seconds_since_progress: float
    command_target_mm: float | None
    command_start_mm: float | None
    command_rpm: int | None
    command_accel: int | None
    safety_blocked: bool
    run_id: str | None
    correlation_id: str | None


MOTION_FLIGHT_RECORDER_SIZE = 256
MOTION_FAULT_DUMP_DIR = Path("data/motion_faults")

class Motor():
    _motor_id: int
    _controller: SharedModbusController
    _params: Any
    _cur_pos: Optional[int]
    _stall_timeout: float
    _status: MotorStatus
    _last_bad_position_log_at: Optional[float]

    def __init__(self, controller: SharedModbusController, params: Any, safety: Optional["MotionSafetyInterlock"] = None) -> None:
        self._motor_id = params.motor_id
        self._controller = controller
        self._params = params
        self._cur_pos = None
        self._stall_timeout = float(getattr(params, "stall_timeout_s", DEFAULT_STALL_TIMEOUT_S))
        self._status = MotorStatus.IDLE
        self._pending_motion: Optional[Callable[[], Awaitable[None]]] = None
        self._target_pos: Optional[float] = None
        self._last_bad_position_log_at = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False
        self._on_move_complete: Optional[Callable[[float], None]] = None
        self._on_init_progress: Optional[Callable[[str, str, Optional[str]], None]] = None
        self._is_homed = False
        self._hard_stopped = False
        self._should_skip_pre_home_once = False
        self._flight_recorder: deque[MotionRecordEntry] = deque(maxlen=MOTION_FLIGHT_RECORDER_SIZE)
        self._diagnostic_run_id: str | None = None
        self._diagnostic_correlation_id: str | None = None
        self._move_started_at: float | None = None
        self._move_start_position: float | None = None
        self._move_target_position: float | None = None
        self._move_rpm: int | None = None
        self._move_accel: int | None = None
        self._last_raw_position: int | None = None
        self._last_position_read_latency_s: float | None = None
        configured_brake_output = getattr(self._params, "brake_output_function", None)
        self._configured_brake_output_function = (
            None if configured_brake_output is None else int(configured_brake_output)
        )
        self._brake_release_delay_ms = int(
            getattr(
                self._params,
                "brake_release_delay_ms",
                DEFAULT_BRAKE_RELEASE_DELAY_MS,
            )
        )
        self._brake_engagement_delay_ms = int(
            getattr(
                self._params,
                "brake_engagement_delay_ms",
                DEFAULT_BRAKE_ENGAGEMENT_DELAY_MS,
            )
        )
        self._brake_engagement_speed_threshold = int(
            getattr(
                self._params,
                "brake_engagement_speed_threshold",
                DEFAULT_BRAKE_ENGAGEMENT_SPEED_THRESHOLD,
            )
        )
        self._active_brake_output_function: int | None = None

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def _emit_init(self, phase: str, detail: str = "", reason: Optional[str] = None) -> None:
        reporter = getattr(self, "_on_init_progress", None)
        if reporter is None:
            return
        try:
            reporter(phase, detail, reason)
        except Exception:
            pass

    async def configure(self):
        try:
            self._emit_init("configuring")
            self._logger.info(
                "Configuring motor",
                extra={"motor_id": self._motor_id, "limit_mm": self._params.lim},
            )

            # Clear any existing alarms
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)

            # Disable auto-homing to prevent blocking motion commands
            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)

            # Configure motor parameters
            if getattr(self._params, "motor_name", None) == "y_axis":
                await self._configure_motor_electrical_parameters()
            else:
                # Preserve the existing configuration behavior for motors whose
                # model-specific encoder and electronic-gearing data was not part
                # of the Y-axis incident review.
                await self.set_param("Peak current", self._params.Ip)

            if self._configured_brake_output_function is not None:
                await self._configure_brake_control()
            max_following_error = getattr(self._params, "max_following_error", 4000)
            await self.set_param("Allowed max position following error", max_following_error)
            await self.set_param("Position loop Kp", 50)
            await self.set_param("Max stop time", 100)
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)

            # Configure movement parameters
            await self.move_config()
        except Exception as e:
            self._emit_init("failed", detail="configure", reason=str(e))
            self._logger.error(
                "Failed to configure motor",
                extra={"motor_id": self._motor_id, "error": str(e)},
                exc_info=True,
            )
            raise MotorConfigError(f"Failed to configure motor {self._motor_id}: {str(e)}") from e

    def cleanup(self) -> None:
        return None

    async def idle(self) -> None:
        try:
            await self.stop()
        except Exception as e:
            self._logger.warning("Failed to idle motor: %s", e)

    async def ready(self) -> None:
        try:
            await self.clear_alarm()
        except Exception as e:
            self._logger.warning("Failed to ready motor: %s", e)

    # ------------------------------------------------------------------ #
    #  Low-level register I/O
    # ------------------------------------------------------------------ #

    async def set_param(self, register: str, value: int) -> None:
        try:
            reg_addr = motor_helpers.get_register_by_name(register)

            if reg_addr is None:
                raise ValueError(f"Invalid register name: {register}")

            await self._controller.write_single_register(
                self._motor_id,
                reg_addr,
                value
            )

        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(f"Failed to set parameter {register}: {str(e)}") from e
            raise

    async def get_param(self, register: str) -> int:
        try:
            reg_addr = motor_helpers.get_register_by_name(register)

            if reg_addr is None:
                raise ValueError(f"Invalid register name: {register}")

            val = await self._controller.read_holding_registers(
                self._motor_id,
                reg_addr
            )

            if val is not None:
                return int(val)

            raise MotorError(f"Failed to read register {register}")

        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(f"Failed to get parameter {register}: {str(e)}") from e
            raise

    async def _read_status(self) -> MotorStatus:
        try:
            status_val = await self._controller.read_holding_registers(
                self._motor_id,
                0x0000
            )
            if status_val is None:
                return MotorStatus.ERROR
            try:
                return MotorStatus(int(status_val))
            except Exception:
                return MotorStatus.ERROR
        except Exception:
            return MotorStatus.ERROR

    # ------------------------------------------------------------------ #
    #  State accessors
    # ------------------------------------------------------------------ #

    def is_homed(self) -> bool:
        return self._is_homed

    def is_hard_stopped(self) -> bool:
        return self._hard_stopped

    def set_diagnostic_context(
        self,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self._diagnostic_run_id = run_id
        self._diagnostic_correlation_id = correlation_id

    def _record_flight_snapshot(
        self,
        snapshot: MotionSnapshot | None,
        alarm_state: MotionAlarmState | None,
        state: MotionWaitState,
        position_delta: float | None,
    ) -> None:
        now = time.monotonic()

        self._flight_recorder.append(
            MotionRecordEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                elapsed_s=now - state.start_time,
                motor_id=self._motor_id,
                motor_name=getattr(self._params, "motor_name", None),
                status_word=None if snapshot is None else snapshot.status_word,
                is_complete=None if snapshot is None else snapshot.is_complete,
                has_fault_bit=None if snapshot is None else snapshot.has_fault_bit,
                alarm_code=None if alarm_state is None else alarm_state.alarm_code,
                alarm_active=None if alarm_state is None else alarm_state.is_active,
                raw_position=self._last_raw_position,
                logical_position_mm=self._cur_pos,
                position_read_latency_s=self._last_position_read_latency_s,
                position_delta_mm=position_delta,
                seconds_since_progress=now - state.last_movement_time,
                command_target_mm=self._move_target_position,
                command_start_mm=self._move_start_position,
                command_rpm=self._move_rpm,
                command_accel=self._move_accel,
                safety_blocked=bool(self._safety and self._safety.blocked),
                run_id=self._diagnostic_run_id,
                correlation_id=self._diagnostic_correlation_id,
            )
        )

    def _dump_flight_recorder(
        self,
        *,
        reason: str,
        state: MotionWaitState,
        snapshot: MotionSnapshot | None = None,
        alarm_state: MotionAlarmState | None = None,
        fault_state: MotionFaultState | None = None,
    ) -> str | None:
        if len(self._flight_recorder) == 0:
            return None

        try:
            MOTION_FAULT_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            motor_name = getattr(self._params, "motor_name", None) or f"motor_{self._motor_id}"
            run_part = self._diagnostic_run_id or "no_run"
            filename = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{run_part}-{motor_name}-{reason}.json"
            path = MOTION_FAULT_DUMP_DIR / filename
            payload = {
                "created_at": now.isoformat(),
                "reason": reason,
                "motor_id": self._motor_id,
                "motor_name": motor_name,
                "run_id": self._diagnostic_run_id,
                "correlation_id": self._diagnostic_correlation_id,
                "target_position_mm": self._target_pos,
                "current_position_mm": self._cur_pos,
                "move_started_at_elapsed_s": None if self._move_started_at is None else time.monotonic() - self._move_started_at,
                "move_start_position_mm": self._move_start_position,
                "move_target_position_mm": self._move_target_position,
                "move_rpm": self._move_rpm,
                "move_accel": self._move_accel,
                "is_homed": self._is_homed,
                "hard_stopped": self._hard_stopped,
                "simulated_fault": self._simulated_fault,
                "stall_timeout_s": self._stall_timeout,
                "seconds_since_progress": time.monotonic() - state.last_movement_time,
                "status_word": None if snapshot is None else snapshot.status_word,
                "alarm_code": None if alarm_state is None else alarm_state.alarm_code,
                "alarm_active": None if alarm_state is None else alarm_state.is_active,
                "fault_state": None if fault_state is None else asdict(fault_state),
                "samples": [asdict(entry) for entry in self._flight_recorder],
            }
            path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
            self._logger.warning(
                "Motion flight recorder dumped",
                extra={
                    "motor_id": self._motor_id,
                    "motor_name": motor_name,
                    "reason": reason,
                    "path": str(path),
                    "run_id": self._diagnostic_run_id,
                    "sample_count": len(self._flight_recorder),
                },
            )
            return str(path)
        except Exception as exc:
            self._logger.warning(
                "Failed to dump motion flight recorder",
                extra={"motor_id": self._motor_id, "reason": reason, "error": str(exc)},
            )
            return None

    async def get_position(self) -> float:
        try:
            read_started_at = time.monotonic()
            reg_high = getattr(self._params, "position_reg_high", 0x602C)
            reg_low = getattr(self._params, "position_reg_low", 0x602D)

            if abs(reg_low - reg_high) == 1:
                reg_start = min(reg_high, reg_low)
                first_reg, second_reg = await self._controller.read_holding_register_block(
                    self._motor_id,
                    reg_start,
                    count=2,
                )

                if reg_high == reg_start:
                    pos_h, pos_l = first_reg, second_reg
                else:
                    pos_l, pos_h = first_reg, second_reg
            else:
                pos_l = await self._controller.read_holding_registers(self._motor_id, reg_low)
                pos_h = await self._controller.read_holding_registers(self._motor_id, reg_high)

            if pos_l is None or pos_h is None:
                raise MotorError(f"Failed to read position registers for motor {self._motor_id}")

            pos = ((pos_h & 0xFFFF) << 16) | (pos_l & 0xFFFF)

            if pos & 0x8000_0000:
                pos -= 0x1_0000_0000

            ppr = getattr(self._params, "ppr", 10000)
            physical_pos = (pos / ppr) * self._params.mpr
            logical_pos = self._params.physical_to_logical(physical_pos)

            max_valid = self._params.lim * 2

            if abs(logical_pos) > max_valid:
                self._logger.debug(
                    "Garbage position detected, retrying",
                    extra={
                        "motor_id": self._motor_id,
                        "raw_pos": pos,
                        "logical_pos": logical_pos,
                        "limit": self._params.lim,
                    },
                )

                await asyncio.sleep(0.1)

                if abs(reg_low - reg_high) == 1:
                    reg_start = min(reg_high, reg_low)
                    first_reg, second_reg = await self._controller.read_holding_register_block(
                        self._motor_id,
                        reg_start,
                        count=2,
                    )

                    if reg_high == reg_start:
                        pos_h, pos_l = first_reg, second_reg
                    else:
                        pos_l, pos_h = first_reg, second_reg
                else:
                    pos_l = await self._controller.read_holding_registers(self._motor_id, reg_low)
                    pos_h = await self._controller.read_holding_registers(self._motor_id, reg_high)

                if pos_l is None or pos_h is None:
                    raise MotorError(f"Failed to re-read position for motor {self._motor_id}")

                pos = ((pos_h & 0xFFFF) << 16) | (pos_l & 0xFFFF)

                if pos & 0x8000_0000:
                    pos -= 0x1_0000_0000

                physical_pos = (pos / ppr) * self._params.mpr
                logical_pos = self._params.physical_to_logical(physical_pos)

                if abs(logical_pos) > max_valid:
                    fallback_pos = self._cur_pos if self._cur_pos is not None else 0.0

                    now = time.monotonic()

                    should_log = (
                        self._last_bad_position_log_at is None
                        or now - self._last_bad_position_log_at >= 60.0
                    )

                    if should_log:
                        self._last_bad_position_log_at = now

                        self._logger.warning(
                            "Garbage position persists after retry, using last known position",
                            extra={
                                "motor_id": self._motor_id,
                                "logical_pos": logical_pos,
                                "fallback_pos": fallback_pos,
                                "has_cached_position": self._cur_pos is not None,
                            },
                        )

                    self._last_raw_position = None
                    self._last_position_read_latency_s = time.monotonic() - read_started_at
                    return fallback_pos

            self._last_raw_position = pos
            self._last_position_read_latency_s = time.monotonic() - read_started_at
            self._cur_pos = logical_pos

            return logical_pos

        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to get position for motor {self._motor_id}: {str(e)}") from e
            raise

    # ------------------------------------------------------------------ #
    #  Brake management
    # ------------------------------------------------------------------ #

    def _expected_motor_electrical_settings(self) -> dict[str, int]:
        return {
            "Control Mode": int(getattr(self._params, "control_mode", 2)),
            "Pulse/revolution": int(getattr(self._params, "ppr", 10000)),
            "Encoder resolution": int(
                getattr(self._params, "encoder_resolution_cpr", 4000)
            ),
            "Peak current": int(self._params.Ip),
        }

    def _validate_y_axis_runtime_profile(self) -> None:
        if getattr(self._params, "motor_name", None) != "y_axis":
            return

        configured_profile = {
            "Ip": int(self._params.Ip),
            "control_mode": int(getattr(self._params, "control_mode", 2)),
            "ppr": int(getattr(self._params, "ppr", 10000)),
            "encoder_resolution_cpr": int(
                getattr(self._params, "encoder_resolution_cpr", 4000)
            ),
            "brake_output_function": self._configured_brake_output_function,
            "brake_release_delay_ms": self._brake_release_delay_ms,
            "brake_engagement_delay_ms": self._brake_engagement_delay_ms,
            "brake_engagement_speed_threshold": self._brake_engagement_speed_threshold,
        }
        mismatches = [
            f"{field}={configured_profile[field]} (expected {expected})"
            for field, expected in Y_AXIS_RUNTIME_PROFILE.items()
            if configured_profile[field] != expected
        ]
        if mismatches:
            raise MotorConfigError(
                "Y-axis runtime profile does not match the installed-machine profile: "
                + ", ".join(mismatches)
            )

    async def _configure_motor_electrical_parameters(self) -> None:
        self._validate_y_axis_runtime_profile()
        settings = self._expected_motor_electrical_settings()

        for parameter_name, value in settings.items():
            await self.set_param(parameter_name, value)

        await self._read_and_verify_motor_electrical_parameters()

    async def _read_and_verify_motor_electrical_parameters(self) -> dict[str, int]:
        settings = self._expected_motor_electrical_settings()
        actual_settings: dict[str, int] = {}

        for parameter_name, expected_value in settings.items():
            actual_value = await self.get_param(parameter_name)
            actual_settings[parameter_name] = actual_value
            if actual_value != expected_value:
                raise MotorConfigError(
                    f"Motor {self._motor_id} {parameter_name} readback mismatch: "
                    f"configured={expected_value}, read={actual_value}"
                )

        return actual_settings

    def require_brake_output_configuration(self) -> int:
        brake_output_function = self._configured_brake_output_function

        if brake_output_function not in BRAKE_SIGNAL_OUTPUT_FUNCTIONS:
            raise MotorConfigError(
                f"Motor {self._motor_id} has no verified brake output configuration; "
                "set brake_output_function to 0x24, 0xA4, or 0xA1"
            )

        return brake_output_function

    def _expected_brake_control_settings(self) -> dict[str, int]:
        return {
            "Brake release delay": self._brake_release_delay_ms,
            "Brake engagement delay": self._brake_engagement_delay_ms,
            "Brake engagement speed threshold": self._brake_engagement_speed_threshold,
        }

    async def _read_and_verify_brake_control(self) -> dict[str, int]:
        configured_function = self.require_brake_output_configuration()
        expected_settings = {
            "SO2 output function": configured_function,
            **self._expected_brake_control_settings(),
        }
        actual_settings: dict[str, int] = {}

        for parameter_name, expected_value in expected_settings.items():
            actual_value = await self.get_param(parameter_name)
            actual_settings[parameter_name] = actual_value
            if actual_value != expected_value:
                raise MotorConfigError(
                    f"Motor {self._motor_id} {parameter_name} readback mismatch: "
                    f"configured={expected_value}, read={actual_value}"
                )

        return actual_settings

    async def _configure_brake_control(self) -> None:
        # Establish and verify the manufacturer's sequencing parameters before
        # assigning SO2, which can immediately change the physical brake state.
        for parameter_name, value in self._expected_brake_control_settings().items():
            await self.set_param(parameter_name, value)

        for parameter_name, expected_value in self._expected_brake_control_settings().items():
            actual_value = await self.get_param(parameter_name)
            if actual_value != expected_value:
                raise MotorConfigError(
                    f"Motor {self._motor_id} {parameter_name} readback mismatch: "
                    f"configured={expected_value}, read={actual_value}"
                )

        await self.set_brake_output_function(
            self.require_brake_output_configuration(),
            force=True,
        )

    async def _verify_brake_output_before_motion_trigger(self) -> None:
        if getattr(self._params, "motor_name", None) != "y_axis":
            return

        configured_function = self.require_brake_output_configuration()
        register_function = await self.get_param("SO2 output function")

        if register_function != configured_function:
            raise MotorConfigError(
                f"Motor {self._motor_id} SO2 readback mismatch: "
                f"configured=0x{configured_function:02X}, read=0x{register_function:02X}"
            )

        self._active_brake_output_function = configured_function

    async def get_brake_output_diagnostics(self) -> dict[str, int]:
        configured_function = self.require_brake_output_configuration()
        motor_settings = await self._read_and_verify_motor_electrical_parameters()
        settings = await self._read_and_verify_brake_control()
        output_state = await self.get_param("Digital output state")

        return {
            "control_mode": motor_settings["Control Mode"],
            "command_pulses_per_revolution": motor_settings["Pulse/revolution"],
            "encoder_resolution_cpr": motor_settings["Encoder resolution"],
            "peak_current_setting": motor_settings["Peak current"],
            "configured_function": configured_function,
            "register_function": settings["SO2 output function"],
            "release_delay_ms": settings["Brake release delay"],
            "engagement_delay_ms": settings["Brake engagement delay"],
            "engagement_speed_threshold": settings[
                "Brake engagement speed threshold"
            ],
            "output_state": output_state,
        }

    async def set_brake_output_function(self, value: int, *, force: bool = False) -> None:
        brake_output_function = int(value)

        if brake_output_function not in BRAKE_SIGNAL_OUTPUT_FUNCTIONS:
            raise MotorConfigError(
                f"Refusing unsupported brake-signal SO2 function 0x{brake_output_function:02X}; "
                "expected 0x24, 0xA4, or 0xA1"
            )

        if force is False and self._active_brake_output_function == brake_output_function:
            return

        await self.set_param("SO2 output function", brake_output_function)
        register_function = await self.get_param("SO2 output function")

        if register_function != brake_output_function:
            raise MotorConfigError(
                f"Motor {self._motor_id} rejected SO2 output function: "
                f"wrote=0x{brake_output_function:02X}, read=0x{register_function:02X}"
            )

        self._active_brake_output_function = brake_output_function

    async def restore_brake_output(self) -> None:
        brake_output_function = self.require_brake_output_configuration()
        await self.set_brake_output_function(brake_output_function, force=True)

    # ------------------------------------------------------------------ #
    #  Alarm / fault management
    # ------------------------------------------------------------------ #

    async def clear_alarm(self) -> None:
        try:
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)
            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)
            await self.move_config()

            alarm_code: Optional[int] = None

            for _ in range(3):
                alarm_code = await self.get_param("Current alarm")
                if alarm_code is None or alarm_code == 0:
                    break
                await asyncio.sleep(0.05)

            if alarm_code is not None and alarm_code != 0:
                raise MotorError(f"Alarm {alarm_code} persists after clear attempt")

            self._status = MotorStatus.STOPPED
        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to clear motor alarm: {str(e)}") from e
            raise

    def set_fault(self) -> None:
        self._simulated_fault = True
        self._status = MotorStatus.FAULT
        self._hard_stopped = True
        self._is_homed = False

        self._logger.warning(
            "Simulated fault triggered",
            extra={"motor_id": self._motor_id},
        )

    # ------------------------------------------------------------------ #
    #  Movement configuration
    # ------------------------------------------------------------------ #

    async def move_config(self) -> None:
        try:
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)

            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)

            await self.set_param("Motion of Path 0", 0x1)
            await self.set_param("Motor direction", self._params.dr)
            await self.set_param("Velocity", self._params.rpm)

        except Exception as e:
            raise MotorConfigError(f"Failed to configure movement parameters: {str(e)}") from e

    def _is_physical_mapping_inverted(self) -> bool:
        p0 = self._params.phys_at_zero if self._params.phys_at_zero is not None else 0
        pm = self._params.phys_at_max if self._params.phys_at_max is not None else self._params.lim
        return p0 > pm

    async def _start_absolute_move(
        self,
        value: float,
        rpm: Optional[int] = None,
        accel: Optional[int] = None,
    ) -> bool:
        original_value = value

        lower_lim = getattr(self._params, "lower_lim", 0)

        if value < lower_lim:
            value = lower_lim
        if value > self._params.lim:
            value = self._params.lim

        if value != original_value:
            self._logger.warning(
                "Target position clamped to motor limits",
                extra={
                    "motor_id": self._motor_id,
                    "requested_mm": original_value,
                    "clamped_mm": value,
                    "limit_mm": self._params.lim,
                },
            )

        if (
            self._cur_pos is not None
            and abs(value - self._cur_pos) <= NOOP_POSITION_TOLERANCE_MM
        ):
            self._logger.info(
                "Motor move skipped because target matches current position",
                extra={
                    "motor_id": self._motor_id,
                    "motor_name": getattr(self._params, "motor_name", None),
                    "position_mm": self._cur_pos,
                    "target_mm": value,
                    "tolerance_mm": NOOP_POSITION_TOLERANCE_MM,
                },
            )
            return False

        self._target_pos = value
        physical_pos = self._params.logical_to_physical(value)

        ppr = getattr(self._params, "ppr", 10000)
        pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(physical_pos, self._params.mpr), ppr)

        if rpm is not None:
            await self.set_param("Velocity", int(rpm))
        if accel is not None:
            await self.set_param("Acceleration", int(accel))
            await self.set_param("Deceleration", int(accel))

        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])

        self._logger.debug(
            "Set position registers",
            extra={
                "motor_id": self._motor_id,
                "target_logical_mm": value,
                "physical_mm": physical_pos,
                "pulse_high": pulse[0],
                "pulse_low": pulse[1],
                "total_pulses": (pulse[0] << 16) | pulse[1],
            }
        )

        await self._verify_brake_output_before_motion_trigger()
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x10
        )

        await asyncio.sleep(0.1)

        self._status = MotorStatus.MOVING
        self._pending_motion = functools.partial(self._start_absolute_move, value, rpm, accel)
        self._move_started_at = time.monotonic()
        self._move_start_position = self._cur_pos
        self._move_target_position = value
        self._move_rpm = int(rpm or self._params.rpm)
        self._move_accel = int(accel or self._params.accel)
        self._flight_recorder.clear()

        self._logger.info(
            "Motor move started",
            extra={
                "motor_id": self._motor_id,
                "motor_name": getattr(self._params, "motor_name", None),
                "start_position_mm": self._move_start_position,
                "target_mm": value,
                "distance_mm": None if self._move_start_position is None else value - self._move_start_position,
                "limit_mm": self._params.lim,
                "rpm": self._move_rpm,
                "accel": self._move_accel,
                "run_id": self._diagnostic_run_id,
                "correlation_id": self._diagnostic_correlation_id,
            }
        )

        return True

    # ------------------------------------------------------------------ #
    #  Public movement API
    # ------------------------------------------------------------------ #

    async def move_to_position(
        self,
        value: float,
        rpm: Optional[int] = None,
        accel: Optional[int] = None,
        *,
        homing: bool = False,
    ) -> None:
        try:
            if self._safety:
                if self._safety.blocked:
                    self._logger.debug(
                        "Safety guard active before move; waiting to clear",
                        extra={"motor_id": self._motor_id, "target_mm": value},
                    )
                await self._safety.wait_clear()

            t0 = time.monotonic()
            did_start = await self._start_absolute_move(value, rpm, accel)
            if did_start is False:
                return
            status = await self.wait_for_move(timeout=MOTOR_MOVE_TIMEOUT, homing=homing)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if status != MotorStatus.STOPPED:
                raise MotorError(
                    f"Movement failed for motor {self._motor_id} (status={status.name})"
                )

            self._hard_stopped = False

        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to move motor {self._motor_id} to position {value}mm: {str(e)}") from e
            raise

    async def move_to_position_no_wait(
        self,
        value: float,
        rpm: Optional[int] = None,
        accel: Optional[int] = None,
    ) -> bool:
        try:
            if self._safety:
                if self._safety.blocked:
                    self._logger.debug(
                        "Safety guard active before move; waiting to clear",
                        extra={"motor_id": self._motor_id, "target_mm": value},
                    )
                await self._safety.wait_clear()

            return await self._start_absolute_move(value, rpm, accel)

        except Exception as e:
            if not isinstance(e, (MotorError, MotorLimitError)):
                raise MotorError(f"Failed to start movement to {value}mm: {str(e)}") from e
            raise

    async def jog(self, dist: float, rpm: Optional[int] = None, homing: bool = False) -> MotorStatus:
        try:
            physical_dist = self._params.logical_to_physical_delta(dist)
            ppr = getattr(self._params, "ppr", 10000)
            pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(physical_dist, self._params.mpr), ppr)

            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)
            await self.set_param("Motion of Path 0", 0x41)
            await self.set_param("Motor direction", self._params.dr)
            await self.set_param("Velocity", rpm or self._params.rpm)
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)
            await self.set_param("Position High", pulse[0])
            await self.set_param("Position Low", pulse[1])

            await self._verify_brake_output_before_motion_trigger()
            await self._controller.write_single_register(
                self._motor_id,
                motor_helpers.TRIGGER,
                0x10
            )

            t0 = time.monotonic()
            result = await self.wait_for_move(timeout=MOTOR_JOG_TIMEOUT, homing=homing)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if result != MotorStatus.STOPPED:
                raise MotorError(f"Relative move failed (status={result.name})")

            await self.move_config()

            return MotorStatus.STOPPED

        except Exception as e:
            if not isinstance(e, (MotorError, MotorTimeoutError)):
                raise MotorError(f"Relative move failed: {str(e)}") from e
            raise

    async def stop(self) -> MotorStatus:
        try:
            await self._controller.write_single_register(
                self._motor_id,
                motor_helpers.TRIGGER,
                0x40
            )
            if self._status in TERMINAL_MOTOR_STATUSES:
                return self._status

            self._status = MotorStatus.STOPPED
            return MotorStatus.STOPPED

        except Exception as e:
            raise MotorError(f"Failed to stop motor {self._motor_id}: {str(e)}") from e

    # ------------------------------------------------------------------ #
    #  Wait / monitor
    # ------------------------------------------------------------------ #

    async def wait_for_move(
        self,
        timeout: float = MOTOR_WAIT_TIMEOUT,
        homing: bool = False,
        should_abort: Callable[[], bool] | None = None,
    ) -> MotorStatus:
        start_time = time.monotonic()
        state = MotionWaitState(
            start_time=start_time,
            last_movement_time=start_time,
        )

        self._logger.debug(
            "Waiting for motor motion",
            extra={"motor_id": self._motor_id, "timeout": timeout, "homing": homing},
        )

        did_cancel_motion = False
        snapshot: MotionSnapshot | None = None
        alarm_state: MotionAlarmState | None = None

        try:
            while True:
                if self._is_move_cancelled(should_abort) is True:
                    await self._cancel_motion(homing=homing)
                    did_cancel_motion = True
                    raise asyncio.CancelledError()

                snapshot = await self._read_motion_snapshot()
                position_delta = await self._update_motion_progress(state)

                if await self._pause_for_safety_if_needed(state):
                    continue

                if snapshot.is_complete and self._is_completion_stable(state):
                    return await self._finish_completed_move(homing, state)

                alarm_state = await self._read_alarm_state(homing, state)
                self._record_flight_snapshot(snapshot, alarm_state, state, position_delta)

                if self._is_homing_limit_stop(homing, alarm_state, state):
                    return await self._finish_homing_limit_stop(snapshot, alarm_state, state)

                fault_state = self._detect_motion_fault(snapshot, alarm_state, state, position_delta)

                if self._is_software_motion_fault_only(snapshot, alarm_state, fault_state) is True:
                    if await self._did_observe_progress_on_fresh_read(state) is True:
                        continue

                if fault_state.is_fault:
                    return await self._finish_fault(homing, snapshot, alarm_state, fault_state, state)

                if time.monotonic() - state.start_time > timeout:
                    return await self._finish_timeout(timeout, homing, state)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            if did_cancel_motion is False:
                await asyncio.shield(self._cancel_motion(homing=homing))
            raise
        except Exception as exc:
            alarm_read_error: str | None = None
            try:
                alarm_state = await self._read_alarm_state(homing, state)
            except Exception as alarm_exc:
                alarm_state = None
                alarm_read_error = str(alarm_exc)

            self._record_flight_snapshot(snapshot, alarm_state, state, None)

            stop_error: str | None = None
            try:
                await self.stop()
            except Exception as stop_exc:
                stop_error = str(stop_exc)
                self._logger.error(
                    "Failed to stop motor after motion monitoring exception",
                    extra={"motor_id": self._motor_id, "error": stop_error},
                )

            if self._status not in TERMINAL_MOTOR_STATUSES:
                self._status = MotorStatus.ERROR
            self._hard_stopped = True
            self._is_homed = False
            self._cur_pos = None
            self._clear_pending_motion()
            dump_path = self._dump_flight_recorder(
                reason="motion_exception",
                state=state,
                snapshot=snapshot,
                alarm_state=alarm_state,
            )
            fault_context = {
                "motor_id": self._motor_id,
                "status_word": None if snapshot is None else snapshot.status_word,
                "alarm_code": None if alarm_state is None else alarm_state.alarm_code,
                "stop_error": stop_error,
                "flight_recorder_path": dump_path,
                "position_known": False,
            }
            if alarm_read_error is not None:
                fault_context["alarm_read_error"] = alarm_read_error
            setattr(exc, "fault_context", fault_context)
            raise

    def _is_completion_stable(self, state: MotionWaitState) -> bool:
        return state.has_moved or time.monotonic() - state.start_time > 0.5

    def _clear_pending_motion(self) -> None:
        self._pending_motion = None
        self._target_pos = None

    async def _stop_and_clear_motion(self) -> None:
        try:
            await self.stop()
        finally:
            self._clear_pending_motion()

    def _is_move_cancelled(self, should_abort: Callable[[], bool] | None) -> bool:
        current_task = asyncio.current_task()

        if should_abort is not None and should_abort() is True:
            return True

        return current_task is not None and current_task.cancelling() > 0

    async def _cancel_motion(self, *, homing: bool = False) -> None:
        if homing is True:
            self._should_skip_pre_home_once = True
            self._is_homed = False
            self._hard_stopped = True

        await self._stop_and_clear_motion()

    async def _raise_if_move_cancelled(
        self,
        should_abort: Callable[[], bool] | None,
        *,
        homing: bool = False,
    ) -> None:
        if self._is_move_cancelled(should_abort) is True:
            await self._cancel_motion(homing=homing)
            raise asyncio.CancelledError()

    async def _read_motion_snapshot(self) -> MotionSnapshot:
        status_reg = getattr(self._params, "status_reg", 0x1003)
        status = await self._controller.read_holding_registers(self._motor_id, status_reg)

        if status is None:
            self._logger.error(
                "Failed to read motion status from motor",
                extra={"motor_id": self._motor_id},
            )

            raise MotorError(f"Failed to read motion status from motor {self._motor_id}")

        complete_bit = getattr(self._params, "status_complete_bit", 5)
        fault_bit_num = getattr(self._params, "status_fault_bit", 0)
        complete = bool(status & (1 << complete_bit))
        fault_bit = bool(status & (1 << fault_bit_num)) if fault_bit_num is not None else False

        return MotionSnapshot(
            status_word=status,
            is_complete=complete,
            has_fault_bit=fault_bit,
        )

    async def _update_motion_progress(self, state: MotionWaitState) -> float | None:
        await self.get_position()
        now = time.monotonic()
        position_delta = None

        if state.last_pos is not None and self._cur_pos is not None:
            position_delta = self._cur_pos - state.last_pos

            if self._is_position_progress(position_delta) is True:
                state.last_movement_time = now
                state.has_moved = True

        state.last_pos = self._cur_pos

        return position_delta

    async def _pause_for_safety_if_needed(self, state: MotionWaitState) -> bool:
        if not self._safety or not self._safety.blocked:
            return False

        pause_started = time.monotonic()

        self._logger.info(
            "Safety guard stop",
            extra={"motor_id": self._motor_id, "position_mm": self._cur_pos},
        )

        await self.stop()
        if self._status in TERMINAL_MOTOR_STATUSES:
            self._clear_pending_motion()
            self._hard_stopped = True
            self._is_homed = False
            raise MotorError(
                f"Motor {self._motor_id} entered terminal state while paused for safety: "
                f"{self._status.name}"
            )

        await self._safety.wait_clear()

        if self._status in TERMINAL_MOTOR_STATUSES:
            self._clear_pending_motion()
            self._hard_stopped = True
            self._is_homed = False
            raise MotorError(
                f"Motor {self._motor_id} entered terminal state while waiting for safety to clear: "
                f"{self._status.name}"
            )

        if self._pending_motion:
            self._logger.info(
                "Safety guard resume",
                extra={
                    "motor_id": self._motor_id,
                    "position_mm": self._cur_pos,
                    "paused_s": time.monotonic() - pause_started,
                },
            )

            await self._pending_motion()

        state.reset(time.monotonic())

        return True

    async def _finish_completed_move(
        self,
        homing: bool,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self.stop()
        self._pending_motion = None

        if self._status in TERMINAL_MOTOR_STATUSES:
            self._target_pos = None
            return self._status

        if self._target_pos is not None:
            current_pos = await self.get_position()

            if self._status in TERMINAL_MOTOR_STATUSES:
                self._target_pos = None
                return self._status

            position_error = abs(current_pos - self._target_pos)

            if position_error > POSITION_TOLERANCE_MM:
                self._logger.warning(
                    "Motor stopped away from target",
                    extra={
                        "motor_id": self._motor_id,
                        "target_mm": self._target_pos,
                        "position_mm": current_pos,
                        "position_error_mm": position_error,
                        "tolerance_mm": POSITION_TOLERANCE_MM,
                    },
                )
                self._target_pos = None
                self._status = MotorStatus.FAULT
                self._hard_stopped = True
                self._is_homed = False
                self._dump_flight_recorder(
                    reason="position_error",
                    state=state,
                )
                return MotorStatus.FAULT

        self._target_pos = None
        if self._status in TERMINAL_MOTOR_STATUSES:
            return self._status

        self._status = MotorStatus.STOPPED
        self._hard_stopped = False

        self._logger.debug(
            "Motion complete",
            extra={
                "motor_id": self._motor_id,
                "homing": homing,
                "elapsed": time.monotonic() - state.start_time,
                "has_moved": state.has_moved,
            }
        )

        return MotorStatus.STOPPED

    async def _read_alarm_state(
        self,
        homing: bool,
        state: MotionWaitState,
    ) -> MotionAlarmState:
        alarm_code: Optional[int] = None
        alarm_active = False

        try:
            alarm_code = await self.get_param("Current alarm")
            if alarm_code is not None and alarm_code != 0:
                if alarm_code == 32:
                    alarm_active = not homing and state.has_moved
                else:
                    alarm_active = True
        except Exception as alarm_exc:
            self._logger.warning(
                "Failed to read motor alarm register",
                extra={"motor_id": self._motor_id, "error": str(alarm_exc)},
            )

            raise

        return MotionAlarmState(alarm_code=alarm_code, is_active=alarm_active)

    def _is_homing_limit_stop(
        self,
        homing: bool,
        alarm_state: MotionAlarmState,
        state: MotionWaitState,
    ) -> bool:
        if not homing or alarm_state.alarm_code != 32:
            return False

        is_at_home_approach_target = False

        if self._cur_pos is not None:
            is_at_home_approach_target = self._is_at_home_approach_target(float(self._cur_pos))

        return state.has_moved is True or is_at_home_approach_target is True

    async def _finish_homing_limit_stop(
        self,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self._stop_and_clear_motion()
        status = self._status if self._status in TERMINAL_MOTOR_STATUSES else MotorStatus.STOPPED

        self._logger.warning(
            "Limit stop detected during homing",
            extra={
                "motor_id": self._motor_id,
                "elapsed": time.monotonic() - state.start_time,
                "status_word": snapshot.status_word,
                "alarm_code": alarm_state.alarm_code,
            },
        )

        return status

    def _is_position_progress(self, position_delta: float | None) -> bool:
        return position_delta is not None and position_delta != 0

    def _detect_motion_fault(
        self,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        state: MotionWaitState,
        position_delta: float | None,
    ) -> MotionFaultState:
        now = time.monotonic()
        did_observe_progress = self._is_position_progress(position_delta)
        no_progress = (
            did_observe_progress is False
            and not state.has_moved
            and (now - state.start_time > max(2.0, self._stall_timeout * 6))
        )
        stall_detected = (
            did_observe_progress is False
            and state.has_moved
            and (now - state.last_movement_time > self._stall_timeout)
        )
        fault = bool(
            snapshot.has_fault_bit
            or alarm_state.is_active
            or stall_detected
            or no_progress
            or self._simulated_fault
        )

        return MotionFaultState(
            is_fault=fault,
            stall_detected=stall_detected,
            no_progress=no_progress,
        )

    def _is_software_motion_fault_only(
        self,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        fault_state: MotionFaultState,
    ) -> bool:
        if fault_state.is_fault is False:
            return False

        if snapshot.has_fault_bit is True or alarm_state.is_active is True or self._simulated_fault is True:
            return False

        return fault_state.stall_detected is True or fault_state.no_progress is True

    async def _did_observe_progress_on_fresh_read(self, state: MotionWaitState) -> bool:
        previous_pos = state.last_pos

        await asyncio.sleep(SOFTWARE_STALL_CONFIRM_DELAY_S)
        await self.get_position()

        now = time.monotonic()
        position_delta = None

        if previous_pos is not None and self._cur_pos is not None:
            position_delta = self._cur_pos - previous_pos

        state.last_pos = self._cur_pos

        if self._is_position_progress(position_delta) is False:
            return False

        state.last_movement_time = now
        state.has_moved = True
        self._record_flight_snapshot(None, None, state, position_delta)

        self._logger.debug(
            "Software stall avoided after fresh position read",
            extra={
                "motor_id": self._motor_id,
                "position_mm": self._cur_pos,
                "position_delta_mm": position_delta,
                "stall_timeout_s": self._stall_timeout,
            },
        )

        return True

    async def _finish_fault(
        self,
        homing: bool,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        fault_state: MotionFaultState,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self.stop()
        terminal_status = self._status if self._status in TERMINAL_MOTOR_STATUSES else MotorStatus.FAULT
        dump_path = self._dump_flight_recorder(
            reason="fault",
            state=state,
            snapshot=snapshot,
            alarm_state=alarm_state,
            fault_state=fault_state,
        )

        if homing:
            self._clear_pending_motion()

            self._logger.warning(
                "Fault/stall detected during homing",
                extra={
                    "motor_id": self._motor_id,
                    "elapsed": time.monotonic() - state.start_time,
                    "status_word": snapshot.status_word,
                    "alarm_code": alarm_state.alarm_code,
                    "stall_detected": fault_state.stall_detected,
                    "no_progress": fault_state.no_progress,
                    "seconds_since_progress": time.monotonic() - state.last_movement_time,
                    "position_mm": self._cur_pos,
                    "target_mm": self._target_pos,
                    "flight_recorder_path": dump_path,
                },
            )

            self._status = terminal_status
            self._hard_stopped = True
            self._is_homed = False

            return terminal_status

        fault_type = "Simulated fault" if self._simulated_fault else "Fault"
        target_pos = self._target_pos
        current_pos = self._cur_pos
        target_error = None if target_pos is None or current_pos is None else target_pos - current_pos

        self._logger.warning(
            f"{fault_type} detected - stopping motor, manual intervention required",
            extra={
                "motor_id": self._motor_id,
                "homing": homing,
                "status_word": snapshot.status_word,
                "position_mm": current_pos,
                "target_mm": target_pos,
                "target_error_mm": target_error,
                "alarm_code": alarm_state.alarm_code,
                "stall_detected": fault_state.stall_detected,
                "no_progress": fault_state.no_progress,
                "seconds_since_progress": time.monotonic() - state.last_movement_time,
                "stall_timeout_s": self._stall_timeout,
                "simulated": self._simulated_fault,
                "flight_recorder_path": dump_path,
                "run_id": self._diagnostic_run_id,
            },
        )

        self._target_pos = None
        self._pending_motion = None
        self._status = terminal_status
        self._hard_stopped = True
        self._is_homed = False

        return terminal_status

    async def _finish_timeout(
        self,
        timeout: float,
        homing: bool,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self.stop()
        terminal_status = self._status if self._status in TERMINAL_MOTOR_STATUSES else MotorStatus.TIMEOUT
        target_pos = self._target_pos
        current_pos = self._cur_pos
        dump_path = self._dump_flight_recorder(
            reason="timeout",
            state=state,
        )

        self._logger.error(
            "Motion timeout",
            extra={
                "motor_id": self._motor_id,
                "timeout": timeout,
                "homing": homing,
                "elapsed": time.monotonic() - state.start_time,
                "position_mm": current_pos,
                "target_mm": target_pos,
                "seconds_since_progress": time.monotonic() - state.last_movement_time,
                "flight_recorder_path": dump_path,
                "run_id": self._diagnostic_run_id,
            }
        )
        self._clear_pending_motion()
        self._status = terminal_status
        self._hard_stopped = True
        self._is_homed = False

        return terminal_status

    # ------------------------------------------------------------------ #
    #  Homing internals
    # ------------------------------------------------------------------ #

    def _get_home_direction(self) -> int:
        return int(self._params.dr) ^ 1 ^ (1 if self._is_physical_mapping_inverted() else 0)

    async def _prepare_home_motion(self, rpm: int, direction: int) -> None:
        await self.set_param("Control word", 0x1111)
        await self.set_param("Control word", 0x1122)
        await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)
        self._status = MotorStatus.STOPPED
        await self.set_param("Motion of Path 0", 0x2)
        await self.set_param("Motor direction", direction)
        await self.set_param("Velocity", rpm)

    async def _start_path_zero(self) -> None:
        await self._verify_brake_output_before_motion_trigger()
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x10,
        )
        self._status = MotorStatus.MOVING

    async def _set_zero_position(self) -> None:
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x21,
        )

    async def _set_and_verify_peak_current(self, value: int) -> None:
        peak_current = int(value)
        if peak_current <= 0:
            raise MotorConfigError("Peak current must be greater than zero")

        await self.set_param("Peak current", peak_current)
        actual_peak_current = await self.get_param("Peak current")
        if actual_peak_current != peak_current:
            raise MotorConfigError(
                f"Motor {self._motor_id} peak current readback mismatch: "
                f"configured={peak_current}, read={actual_peak_current}"
            )

    def _get_home_approach_target_mm(self) -> float:
        backoff_mm = DEFAULT_HOME_APPROACH_BACKOFF_MM
        lower_lim = float(getattr(self._params, "lower_lim", 0.0))
        upper_lim = float(self._params.lim)

        if self._is_physical_mapping_inverted() is True:
            return max(lower_lim, upper_lim - backoff_mm)

        return min(upper_lim, max(lower_lim, lower_lim + backoff_mm))

    def _is_at_home_approach_target(self, position_mm: float) -> bool:
        target_mm = self._get_home_approach_target_mm()

        if self._is_physical_mapping_inverted() is True:
            return position_mm >= target_mm - HOME_APPROACH_TOLERANCE_MM

        return position_mm <= target_mm + HOME_APPROACH_TOLERANCE_MM

    def should_move_near_home_before_homing(self) -> bool:
        return getattr(self._params, "motor_name", None) != "stage"

    def _should_skip_pre_home_after_interrupted_homing(self) -> bool:
        if self._should_skip_pre_home_once is False:
            return False

        self._should_skip_pre_home_once = False
        self._logger.info(
            "Skipping pre-home approach after interrupted homing",
            extra={"motor_id": self._motor_id},
        )
        return True

    async def _move_near_home(self, rpm: int) -> None:
        if self._should_skip_pre_home_after_interrupted_homing() is True:
            return

        target_mm = self._get_home_approach_target_mm()
        current_position_mm = await self.get_position()
        should_move_near_home = self.should_move_near_home_before_homing()

        if should_move_near_home is False:
            return

        if self._is_at_home_approach_target(float(current_position_mm)) is True:
            return

        await self.move_to_position(target_mm, rpm=rpm, homing=False)

    async def _move_away_from_home(self, distance_mm: float, rpm: int) -> None:
        if distance_mm <= 0:
            raise MotorConfigError("Pre-home clearance distance must be positive")

        logical_distance_mm = -distance_mm if self._is_physical_mapping_inverted() is True else distance_mm
        self._logger.info(
            "Starting pre-home clearance move",
            extra={
                "motor_id": self._motor_id,
                "motor_name": getattr(self._params, "motor_name", None),
                "distance_mm": logical_distance_mm,
                "rpm": rpm,
            },
        )
        await self.jog(logical_distance_mm, rpm=rpm, homing=False)

    # ------------------------------------------------------------------ #
    #  Public homing API
    # ------------------------------------------------------------------ #

    async def home(
        self,
        pre_home_clearance_mm: Optional[float] = None,
        *,
        skip_pre_home_approach: bool = False,
        homing_peak_current: Optional[int] = None,
    ) -> None:
        self._simulated_fault = False
        restore_peak_current: int | None = None

        try:
            self._emit_init("homing")
            await self.clear_alarm()

            home_dir = self._get_home_direction()
            home_rpm = self._params.home_rpm or self._params.ret_rpm

            try:
                if pre_home_clearance_mm is not None:
                    await self._move_away_from_home(pre_home_clearance_mm, self._params.ret_rpm)
                if skip_pre_home_approach is True:
                    self._logger.warning(
                        "Skipping pre-home approach for diagnostic homing",
                        extra={
                            "motor_id": self._motor_id,
                            "motor_name": getattr(self._params, "motor_name", None),
                        },
                    )
                else:
                    await self._move_near_home(self._params.ret_rpm)
            except Exception as e:
                raise MotorConfigError(f"Failed during pre-home approach: {str(e)}") from e

            try:
                await self._prepare_home_motion(home_rpm, home_dir)
            except Exception as e:
                raise MotorConfigError(f"Failed to configure homing parameters: {str(e)}") from e

            if homing_peak_current is not None:
                restore_peak_current = int(self._params.Ip)
                self._logger.warning(
                    "Applying temporary homing peak current",
                    extra={
                        "motor_id": self._motor_id,
                        "motor_name": getattr(self._params, "motor_name", None),
                        "homing_peak_current": int(homing_peak_current),
                        "restore_peak_current": restore_peak_current,
                    },
                )
                await self._set_and_verify_peak_current(int(homing_peak_current))

            try:
                await self._start_path_zero()
            except Exception as e:
                raise MotorError(f"Failed to start homing movement: {str(e)}") from e

            t0 = time.monotonic()
            stop = await self.wait_for_move(timeout=MOTOR_HOME_TIMEOUT, homing=True)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if stop == MotorStatus.STOPPED:
                try:
                    await self._set_zero_position()
                except Exception as e:
                    raise MotorError(f"Failed to set home position as zero: {str(e)}") from e

                await self.move_config()

                self._is_homed = True
                self._hard_stopped = False
                self._should_skip_pre_home_once = False
                self._emit_init("ready")
            elif stop == MotorStatus.FAULT:
                self._is_homed = False
                self._hard_stopped = True

                raise MotorError(f"Motor {self._motor_id} faulted during homing")
            else:
                self._is_homed = False

                raise MotorTimeoutError(f"Motor {self._motor_id} homing timed out")

        except Exception as e:
            if isinstance(e, MotorTimeoutError):
                self._is_homed = False
            reason = "timeout" if isinstance(e, MotorTimeoutError) else str(e)
            self._emit_init("failed", detail="home", reason=reason)
            if not isinstance(e, (MotorError, MotorConfigError, MotorTimeoutError)):
                raise MotorError(f"Unexpected error during homing: {str(e)}") from e
            raise
        finally:
            if restore_peak_current is not None:
                try:
                    await self._set_and_verify_peak_current(restore_peak_current)
                    self._logger.warning(
                        "Restored peak current after temporary homing override",
                        extra={
                            "motor_id": self._motor_id,
                            "motor_name": getattr(self._params, "motor_name", None),
                            "peak_current": restore_peak_current,
                        },
                    )
                except Exception as restore_error:
                    self._is_homed = False
                    self._hard_stopped = True
                    self._emit_init("failed", detail="home", reason="peak current restore failed")
                    raise MotorConfigError(
                        f"Failed to restore motor {self._motor_id} peak current "
                        f"to {restore_peak_current}: {restore_error}"
                    ) from restore_error
