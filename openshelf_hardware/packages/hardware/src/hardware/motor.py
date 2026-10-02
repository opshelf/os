from __future__ import annotations

import time
import asyncio
import functools
import logging
import json
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Callable, Awaitable

from hardware import motor_helpers
from hardware.controller import SharedModbusController

if TYPE_CHECKING:
    from hardware.curtain import MotionSafetyInterlock
    from hardware.gpio.types import DigitalInput

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
ALARM_DETECT_FOLLOWING_ERROR_BIT = 2
# Drive torque homing (Pr8.10 method 3 in bits 2-3, CL2B manual / MotionStudio;
# the CL57RS table omits it, so every write is read back). The drive drives into
# the stop at Pr8.20 % current, holds Pr8.19 ms, zeroes, and sets 0x1003 bit6.
HOMING_METHOD_TORQUE = 0x3 << 2
HOMING_TRIGGER = 0x20
MOTION_STATUS_HOMED_BIT = 6
DEFAULT_HOME_TORQUE_PCT = 30
DEFAULT_HOME_TORQUE_MS = 200
# Pr8.21 overtravel bound, in 0.1 rev, as a multiple of the axis travel.
HOMING_OVERTRAVEL_MARGIN = 1.25
ZERO_TOLERANCE_MM = 0.25
ZERO_SETTLE_TIMEOUT_S = 1.0
ZERO_SETTLE_POLL_S = 0.02
MOTION_STATUS_FAULT_BIT = 0
MOTION_STATUS_RUNNING_BIT = 2
HOME_LIMIT_VERIFY_TIMEOUT_S = 0.5
HOME_LIMIT_VERIFY_POLL_INTERVAL_S = 0.02
# SO2 as the drive's own brake output. 0x24 is BRAKE with no inversion: the
# output is open-collector common-anode, so asserted sinks the line, energises
# the relay and releases the brake. 0xA4 inverts that. 0xA1 (inverted
# path-complete) is deliberately absent -- it is a status signal, untied to the
# enable state, and it leaves Pr4.19/4.20/4.21 inert.
BRAKE_OUTPUT_FUNCTIONS = frozenset({0x24, 0xA4})
DEFAULT_BRAKE_RELEASE_DELAY_MS = 250
DEFAULT_BRAKE_ENGAGEMENT_DELAY_MS = 250
DEFAULT_BRAKE_ENGAGEMENT_SPEED_THRESHOLD = 10
DEFAULT_STALL_TIMEOUT_S = 5.0
SOFTWARE_STALL_CONFIRM_DELAY_S = 0.05

# Axes that give up their enable between moves so the load rests on the brake
# rather than on holding current. Only Y hangs its load off the motor.
PARKED_AXES = frozenset({"y_axis"})

# Pr4.02 on a parked axis: DI1 inert, N.C. polarity retained. With no input
# carrying the ENABLE function, Pr0.07 becomes the sole enable source, which is
# what makes a software disable mean anything -- Pr0.07 is a *forced* enable and
# at 0 it falls back to whichever input holds ENABLE.
ENABLE_INPUT_INERT = 0x0080
SOFTWARE_ENABLE_ON = 1
SOFTWARE_ENABLE_OFF = 0
MOTION_STATUS_ENABLE_BIT = 1
# The drive keeps current for Pr4.20 while the brake sets, so disable waits that
# long plus this margin before calling the drive stuck enabled.
DISABLE_CONFIRM_MARGIN_S = 0.25
DISABLE_POLL_S = 0.02
# Relay and armature lag on top of Pr4.19, and how long Pr0.07=1 may take to
# show as enabled before motion is refused.
BRAKE_RELEASE_MARGIN_S = 0.1
ENABLE_CONFIRM_TIMEOUT_S = 0.5
EEPROM_SAVE_COMMAND = 0x2211
EEPROM_SAVE_OK = 0x5555
EEPROM_SAVE_FAILED = 0xAAAA
EEPROM_SAVE_POLL_ATTEMPTS = 10
EEPROM_SAVE_POLL_DELAY_S = 0.05

Y_AXIS_RUNTIME_PROFILE = {
    "Ip": 60,
    "control_mode": 2,
    "ppr": 10000,
    "encoder_resolution_cpr": 4000,
    "brake_output_function": 0x24,
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
    # Drive homing: seen bit6 clear or running, so a stale bit6 is not success.
    homing_started: bool = False

    def reset(self, now: float) -> None:
        self.start_time = now
        self.last_movement_time = now
        self.last_pos = None
        self.has_moved = False
        self.homing_started = False


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
MOTION_FAULT_DUMP_MAX_FILES = 256
MOTION_FAULT_DUMP_MAX_AGE_DAYS = 30.0


def _prune_motion_fault_dumps(
    directory: Path,
    *,
    max_files: int = MOTION_FAULT_DUMP_MAX_FILES,
    max_age_days: float = MOTION_FAULT_DUMP_MAX_AGE_DAYS,
    now: float | None = None,
) -> int:
    """Delete old flight recorder dumps so the dump directory stays bounded.

    Removes dumps older than max_age_days, then the oldest (by filename
    timestamp) beyond max_files. Returns the number of files deleted.
    """
    if not directory.is_dir():
        return 0
    timestamp = time.time() if now is None else now
    cutoff = timestamp - max_age_days * 86400.0
    entries = [
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix == ".json"
    ]
    if not entries:
        return 0

    def _mtime(entry: Path) -> float:
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0

    doomed = {entry.name for entry in entries if _mtime(entry) < cutoff}
    survivors = sorted(
        (entry for entry in entries if entry.name not in doomed),
        key=lambda entry: entry.name,
    )
    if len(survivors) > max_files:
        for entry in survivors[: len(survivors) - max_files]:
            doomed.add(entry.name)

    deleted = 0
    for entry in entries:
        if entry.name not in doomed:
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError:
            continue
    return deleted

class Motor():
    _motor_id: int
    _controller: SharedModbusController
    _params: Any
    _cur_pos: Optional[int]
    _stall_timeout: float
    _status: MotorStatus
    _last_bad_position_log_at: Optional[float]

    def __init__(
        self,
        controller: SharedModbusController,
        params: Any,
        safety: Optional["MotionSafetyInterlock"] = None,
        home_limit_input: Optional["DigitalInput"] = None,
    ) -> None:
        self._motor_id = params.motor_id
        self._controller = controller
        self._params = params
        self._home_limit_input = home_limit_input
        self._cur_pos = None
        self._stall_timeout = float(getattr(params, "stall_timeout_s", DEFAULT_STALL_TIMEOUT_S))
        self._status = MotorStatus.IDLE
        self._pending_motion: Optional[Callable[[], Awaitable[None]]] = None
        self._safety_interrupted_move = False
        self._target_pos: Optional[float] = None
        self._last_fault_context: dict[str, Any] | None = None
        self._last_bad_position_log_at = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False
        self._on_move_complete: Optional[Callable[[float], None]] = None
        self._on_init_progress: Optional[Callable[[str, str, Optional[str]], None]] = None
        self._is_homed = False
        self._hard_stopped = False
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
        self._disabled_at: float | None = None
        self._hold_enabled = False
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

    def is_moving(self) -> bool:
        """Report whether a move is in flight, without touching the bus.

        Cleared as soon as the drive is told to stop, including the stop that a
        safety interruption causes, so it is a pause-aware motion flag.
        """
        return self._status is MotorStatus.MOVING

    def position_mm(self) -> Optional[float]:
        """Last position tracked in-process, without touching the bus."""
        return self._cur_pos


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
            pruned = _prune_motion_fault_dumps(MOTION_FAULT_DUMP_DIR)
            if pruned > 0:
                self._logger.warning(
                    "Motion flight recorder dumps pruned",
                    extra={"motor_id": self._motor_id, "deleted_count": pruned},
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

        if brake_output_function not in BRAKE_OUTPUT_FUNCTIONS:
            raise MotorConfigError(
                f"Motor {self._motor_id} has no verified brake output configuration; "
                "set brake_output_function to 0x24 or 0xA4"
            )

        return brake_output_function

    def _expected_brake_control_settings(self) -> dict[str, int]:
        return {
            "Brake release delay": self._brake_release_delay_ms,
            "Brake engagement delay": self._brake_engagement_delay_ms,
            "Brake engagement speed threshold": self._brake_engagement_speed_threshold,
        }

    def _expected_persistent_brake_config(self) -> dict[str, int]:
        """The registers the drive must hold in EEPROM for brake control to work.

        Nothing at runtime writes any of these, so once the drive agrees with
        this the boot audit is a pure read and the EEPROM is never touched
        again. Peak current and Pr0.07 are deliberately absent: both are moved
        at runtime, so auditing them would see a legitimate value and provoke a
        needless save.
        """
        settings = {
            **self._expected_brake_control_settings(),
            "SO2 output function": self.require_brake_output_configuration(),
        }

        if self._park_when_idle:
            settings["SI1 input function"] = ENABLE_INPUT_INERT

        return settings

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

    async def _audit_persistent_brake_config(self) -> dict[str, int]:
        expected = self._expected_persistent_brake_config()
        mismatches: dict[str, int] = {}

        for parameter_name, expected_value in expected.items():
            actual_value = await self.get_param(parameter_name)
            if actual_value != expected_value:
                mismatches[parameter_name] = actual_value

        return mismatches

    async def _save_parameters_to_eeprom(self) -> None:
        await self.set_param("Control word", EEPROM_SAVE_COMMAND)

        for _ in range(EEPROM_SAVE_POLL_ATTEMPTS):
            status = await self.get_param("Save status word")

            if status == EEPROM_SAVE_OK:
                return
            if status == EEPROM_SAVE_FAILED:
                raise MotorConfigError(
                    f"Motor {self._motor_id} reported an EEPROM save failure (0xAAAA)"
                )

            await asyncio.sleep(EEPROM_SAVE_POLL_DELAY_S)

        raise MotorConfigError(
            f"Motor {self._motor_id} did not confirm the EEPROM save within "
            f"{EEPROM_SAVE_POLL_ATTEMPTS * EEPROM_SAVE_POLL_DELAY_S:.2f}s"
        )

    async def _repair_persistent_brake_config(self) -> None:
        """Write the persistent brake config, then commit it to EEPROM.

        The write order matters for a suspended load. Pr0.07 goes high first so
        the motor stays energised across the transition; the sequencing delays
        land before SO2 becomes a brake output, because assigning SO2 can change
        the physical brake state immediately; DI1 is made inert last, since from
        that point Pr0.07 is the only thing that can enable the drive.
        """
        if self._park_when_idle:
            await self.set_param("Forced enable by software", SOFTWARE_ENABLE_ON)

        for parameter_name, value in self._expected_brake_control_settings().items():
            await self.set_param(parameter_name, value)

        await self.set_param(
            "SO2 output function",
            self.require_brake_output_configuration(),
        )

        if self._park_when_idle:
            await self.set_param("SI1 input function", ENABLE_INPUT_INERT)

        remaining = await self._audit_persistent_brake_config()
        if remaining:
            raise MotorConfigError(
                f"Motor {self._motor_id} rejected the brake configuration: "
                + ", ".join(
                    f"{name} read={value}" for name, value in remaining.items()
                )
            )

        await self._save_parameters_to_eeprom()

        if self._park_when_idle:
            # Deliberate park: the brake sets through Pr4.20 / Pr4.21.
            await self.set_param("Forced enable by software", SOFTWARE_ENABLE_OFF)
            self._disabled_at = time.monotonic()

    async def _configure_brake_control(self) -> None:
        """Audit the drive's persistent brake config; repair and save only if wrong.

        Steady state is a read of four or five registers and nothing else. The
        EEPROM is written only when the drive genuinely disagrees, which after
        commissioning means never.
        """
        mismatches = await self._audit_persistent_brake_config()

        if not mismatches:
            self._logger.info(
                "Persistent brake configuration matches; no write required",
                extra={
                    "motor_id": self._motor_id,
                    "motor_name": getattr(self._params, "motor_name", None),
                },
            )
            return

        expected = self._expected_persistent_brake_config()
        self._logger.warning(
            "Persistent brake configuration mismatch; repairing and saving to EEPROM",
            extra={
                "motor_id": self._motor_id,
                "motor_name": getattr(self._params, "motor_name", None),
                "mismatches": {
                    name: {"expected": expected[name], "read": value}
                    for name, value in mismatches.items()
                },
            },
        )

        await self._repair_persistent_brake_config()

        self._logger.warning(
            "Persistent brake configuration repaired and saved to EEPROM",
            extra={
                "motor_id": self._motor_id,
                "motor_name": getattr(self._params, "motor_name", None),
            },
        )

        if "SI1 input function" in mismatches:
            # Manual 4.3.2 note 2: an input function change only takes effect
            # after the drive restarts. Until then DI1 still carries ENABLE and
            # Pr0.07=0 falls back to it, so the axis will not park.
            self._logger.warning(
                "DI1 function changed; POWER-CYCLE THE DRIVE for it to take "
                "effect. Until then the software disable will not de-energise "
                "this axis and the load stays on motor current",
                extra={
                    "motor_id": self._motor_id,
                    "motor_name": getattr(self._params, "motor_name", None),
                    "previous_function": mismatches["SI1 input function"],
                    "configured_function": ENABLE_INPUT_INERT,
                },
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

    async def get_brake_output_diagnostics(self) -> dict[str, int]:
        configured_function = self.require_brake_output_configuration()
        motor_settings = await self._read_and_verify_motor_electrical_parameters()
        settings = await self._read_and_verify_brake_control()
        output_state = await self.get_param("Digital output state")
        software_enable = await self.get_param("Forced enable by software")
        enable_input_function = await self.get_param("SI1 input function")
        input_state = await self.get_param("Digital input state")
        drive_enabled = await self.is_drive_enabled()

        return {
            "enable_input_function": enable_input_function,
            "software_enable": software_enable,
            "digital_input_state": input_state,
            "drive_enabled": int(drive_enabled),
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

    # ------------------------------------------------------------------ #
    #  Enable / park
    # ------------------------------------------------------------------ #

    @property
    def _park_when_idle(self) -> bool:
        return getattr(self._params, "motor_name", None) in PARKED_AXES

    @property
    def _brake_release_s(self) -> float:
        return self._brake_release_delay_ms / 1000.0

    async def is_drive_enabled(self) -> bool:
        """Bit1 of the motion status word: what the drive thinks its enable is."""
        status = await self.get_param("Motion status")
        return bool(status & (1 << MOTION_STATUS_ENABLE_BIT))

    async def enable(self) -> None:
        """Energise, then give the drive its own Pr4.19 to release the brake.

        The wait is not the host sequencing the brake -- nothing here commands
        it. It is the host declining to command motion until the drive has had
        the time it was configured to need. Firing PR0 into a brake that is
        still set is the failure this axis already has a runbook for. A recent
        disable is first allowed to finish its Pr4.20 engagement, and the
        enable is read back before any dwell counts.
        """
        if self._disabled_at is not None:
            settle_s = (
                self._disabled_at
                + self._brake_engagement_delay_ms / 1000.0
                + DISABLE_CONFIRM_MARGIN_S
                - time.monotonic()
            )
            if settle_s > 0:
                await asyncio.sleep(settle_s)

        await self.set_param("Forced enable by software", SOFTWARE_ENABLE_ON)

        deadline = time.monotonic() + ENABLE_CONFIRM_TIMEOUT_S
        while await self.is_drive_enabled() is False:
            if time.monotonic() > deadline:
                raise MotorError(f"Motor {self._motor_id} did not report enabled after Pr0.07=1")
            await asyncio.sleep(DISABLE_POLL_S)

        await asyncio.sleep(self._brake_release_s + BRAKE_RELEASE_MARGIN_S)
        self._disabled_at = None

    async def disable(self) -> None:
        """De-energise. The drive holds current for Pr4.20 while the brake sets.

        Pr0.07 is a *forced* enable: at 0 the enable falls back to whichever
        digital input carries ENABLE. If any input still holds it -- or the
        drive declines to gate on IO at all -- the write lands and changes
        nothing, so the result is read back rather than assumed. This warns
        rather than raises: a drive that will not de-energise is still holding
        the load, which is not itself dangerous.
        """
        await self.set_param("Forced enable by software", SOFTWARE_ENABLE_OFF)
        self._disabled_at = time.monotonic()

        deadline = time.monotonic() + self._brake_engagement_delay_ms / 1000.0 + DISABLE_CONFIRM_MARGIN_S
        while await self.is_drive_enabled() is True:
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(DISABLE_POLL_S)
        else:
            return

        self._logger.warning(
            "Drive still reports enabled after Pr0.07=0; the load is on motor "
            "current, not the brake",
            extra={
                "motor_id": self._motor_id,
                "motor_name": getattr(self._params, "motor_name", None),
                "enable_input_function": await self.get_param("SI1 input function"),
                "digital_input_state": await self.get_param("Digital input state"),
                "digital_output_state": await self.get_param("Digital output state"),
            },
        )

    async def _enable_for_motion(self) -> None:
        if self._park_when_idle is False:
            return

        if self._hold_enabled is True and await self.is_drive_enabled() is True:
            return

        await self.enable()

    @asynccontextmanager
    async def hold_enabled(self):
        """Keep a parked axis energised across moves; park once on exit."""
        if self._park_when_idle is False or self._hold_enabled is True:
            yield
            return

        self._hold_enabled = True
        try:
            await self.enable()
            yield
        finally:
            self._hold_enabled = False
            await self._park()

    async def _park(self) -> None:
        """Disable after motion, tolerating a bus that has already gone wrong.

        A failure to park is worth a warning and nothing more: the axis is left
        energised, which is the conservative state for a suspended load, and the
        caller's outcome is never masked. A hold defers the park to its exit.
        """
        if self._park_when_idle is False or self._hold_enabled is True:
            return

        try:
            await self.disable()
        except Exception as e:
            self._logger.warning(
                "Failed to park motor; left energised",
                extra={
                    "motor_id": self._motor_id,
                    "motor_name": getattr(self._params, "motor_name", None),
                    "error": str(e),
                },
            )

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
        await self._enable_for_motion()
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
            self._last_fault_context = None
            did_start = await self._start_absolute_move(value, rpm, accel)
            if did_start is False:
                return
            status = await self.wait_for_move(timeout=MOTOR_MOVE_TIMEOUT)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if status != MotorStatus.STOPPED:
                error = MotorError(
                    f"Movement failed for motor {self._motor_id} (status={status.name})"
                )
                if self._last_fault_context is not None:
                    setattr(error, "fault_context", self._last_fault_context)
                raise error

            self._hard_stopped = False

        except Exception as e:
            if not isinstance(e, MotorError):
                error = MotorError(
                    f"Failed to move motor {self._motor_id} to position {value}mm: {str(e)}"
                )
                fault_context = getattr(e, "fault_context", None)
                if isinstance(fault_context, dict):
                    setattr(error, "fault_context", fault_context)
                raise error from e
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

    async def jog(self, dist: float, rpm: Optional[int] = None) -> MotorStatus:
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

            self._safety_interrupted_move = False
            await self._verify_brake_output_before_motion_trigger()
            await self._enable_for_motion()
            await self._controller.write_single_register(
                self._motor_id,
                motor_helpers.TRIGGER,
                0x10
            )
            # Deliberately no _pending_motion here: a relative jog cannot be
            # restarted from the current position without travelling the full
            # distance again, so a safety pause stops the drive and jog() fails the
            # move instead of reporting the drive's path-complete bit for a move it
            # stopped halfway. The status still has to say MOVING or anything
            # watching this axis, such as the weight-change guard, reads the
            # pre-home clearance jog as idle.
            self._status = MotorStatus.MOVING

            t0 = time.monotonic()
            result = await self.wait_for_move(timeout=MOTOR_JOG_TIMEOUT)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if result != MotorStatus.STOPPED:
                raise MotorError(f"Relative move failed (status={result.name})")

            if self._safety_interrupted_move is True:
                self._safety_interrupted_move = False

                raise MotorError(
                    "Relative move was stopped by the safety guard and did not complete"
                )

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

                snapshot = await self._read_motion_snapshot(homing)
                position_delta = await self._update_motion_progress(state)

                if await self._pause_for_safety_if_needed(state):
                    continue

                if homing and not snapshot.is_complete:
                    state.homing_started = True

                if snapshot.is_complete and self._is_completion_stable(state, homing):
                    return await self._finish_completed_move(homing, state)

                alarm_state = await self._read_alarm_state(homing, state)
                self._record_flight_snapshot(snapshot, alarm_state, state, position_delta)

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
                "stall_detected": None,
                "no_progress": None,
                "safety_blocked": bool(self._safety and self._safety.blocked),
                "stop_error": stop_error,
                "flight_recorder_path": dump_path,
                "position_known": False,
            }
            if alarm_read_error is not None:
                fault_context["alarm_read_error"] = alarm_read_error
            setattr(exc, "fault_context", fault_context)
            raise

    def _is_completion_stable(self, state: MotionWaitState, homing: bool = False) -> bool:
        if homing:
            return state.homing_started
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

    async def _read_motion_snapshot(self, homing: bool = False) -> MotionSnapshot:
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
        if homing:
            homed = bool(status & (1 << MOTION_STATUS_HOMED_BIT))
            complete = homed and not status & (1 << MOTION_STATUS_RUNNING_BIT)
        else:
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

        else:
            # Nothing to restart. A relative jog cannot be re-triggered from where
            # it stopped without travelling the full distance again, so the caller
            # has to be told the move was interrupted rather than shown the drive's
            # path-complete bit for a move that was stopped halfway.
            self._safety_interrupted_move = True

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

        # Homing parks in home(), after the zero and limit checks.
        if homing is False:
            await self._park()

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
                    alarm_active = homing or state.has_moved
                else:
                    alarm_active = True
        except Exception as alarm_exc:
            self._logger.warning(
                "Failed to read motor alarm register",
                extra={"motor_id": self._motor_id, "error": str(alarm_exc)},
            )

            raise

        return MotionAlarmState(alarm_code=alarm_code, is_active=alarm_active)

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
        self._last_fault_context = {
            "motor_id": self._motor_id,
            "status_word": snapshot.status_word,
            "alarm_code": alarm_state.alarm_code,
            "stall_detected": fault_state.stall_detected,
            "no_progress": fault_state.no_progress,
            "safety_blocked": bool(self._safety and self._safety.blocked),
            "flight_recorder_path": dump_path,
        }

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

    def _homing_direction_bit(self) -> int:
        """Pr8.10 bit0 with Pr0.03 = dr: 0 seeks the logical-zero end, 1 the far end."""
        return 1 if self._is_physical_mapping_inverted() is True else 0

    def _homing_overtravel(self) -> int:
        """Pr8.21 in 0.1 rev: the farthest start from the drive zero plus margin, so a missed stop fails."""
        p0 = self._params.phys_at_zero if self._params.phys_at_zero is not None else 0
        pm = self._params.phys_at_max if self._params.phys_at_max is not None else self._params.lim
        travel_mm = float(max(p0, pm))
        value = round(travel_mm / self._params.mpr * 10 * HOMING_OVERTRAVEL_MARGIN)
        return max(1, min(value, 0xFFFF))

    async def _write_verified(self, settings: dict[str, int]) -> None:
        for name, value in settings.items():
            await self.set_param(name, value)

        for name, value in settings.items():
            actual = await self.get_param(name)
            if actual != value:
                raise MotorConfigError(
                    f"Motor {self._motor_id} {name} readback mismatch: "
                    f"configured={value}, read={actual}"
                )

    async def _configure_drive_homing(self, alarm_detection: int) -> None:
        """Load Pr8.10-8.21 for torque homing, with following-error alarms off."""
        p = self._params
        rpm = p.home_rpm or p.ret_rpm
        await self._write_verified({
            "Alarm detection": alarm_detection & ~(1 << ALARM_DETECT_FOLLOWING_ERROR_BIT),
            "Homing mode": HOMING_METHOD_TORQUE | self._homing_direction_bit(),
            "Homing high velocity": rpm,
            "Homing low velocity": rpm,
            "Homing acceleration": p.accel,
            "Homing deceleration": p.accel,
            "Homing torque time": int(getattr(p, "home_torque_ms", DEFAULT_HOME_TORQUE_MS)),
            "Homing torque percent": int(getattr(p, "home_torque_pct", DEFAULT_HOME_TORQUE_PCT)),
            "Homing overtravel": self._homing_overtravel(),
        })

    async def _trigger_drive_homing(self) -> None:
        await self._verify_brake_output_before_motion_trigger()
        await self._enable_for_motion()
        await self._controller.write_single_register(self._motor_id, motor_helpers.TRIGGER, HOMING_TRIGGER)
        self._status = MotorStatus.MOVING

    async def _read_pr_warning(self) -> int | None:
        try:
            return await self.get_param("PR warning")
        except Exception:
            return None

    async def _verify_zero_position(self) -> None:
        """Fail homing if the drive did not zero its own counter at the stop.

        The check is on the drive's physical position: the logical mapping puts
        the drive's zero at lim on an inverted axis and off zero on an offset one.
        """
        ppr = getattr(self._params, "ppr", 10000)
        deadline = time.monotonic() + ZERO_SETTLE_TIMEOUT_S
        while True:
            # Also refreshes the cached logical position; the raw count is the drive's.
            await self.get_position()
            raw_position = self._last_raw_position
            position = None if raw_position is None else (raw_position / ppr) * self._params.mpr
            if position is not None and abs(position) <= ZERO_TOLERANCE_MM:
                return

            if time.monotonic() > deadline:
                shown = "unreadable" if position is None else f"{position:.3f} mm"
                raise MotorError(f"Motor {self._motor_id} zero did not take: position={shown}")

            await asyncio.sleep(ZERO_SETTLE_POLL_S)

    async def _verify_home_limit_engaged(self) -> None:
        if self._home_limit_input is None:
            return

        deadline = time.monotonic() + HOME_LIMIT_VERIFY_TIMEOUT_S
        while True:
            if self._home_limit_input.read() is True:
                return

            if time.monotonic() >= deadline:
                break

            await asyncio.sleep(HOME_LIMIT_VERIFY_POLL_INTERVAL_S)

        self._is_homed = False
        self._hard_stopped = True
        raise MotorLimitError(
            f"Motor {self._motor_id} completed homing but limit switch "
            f"'{self._home_limit_input.name}' is not engaged"
        )

    # ------------------------------------------------------------------ #
    #  Public homing API
    # ------------------------------------------------------------------ #

    async def home(self) -> None:
        """Torque-home on the drive; it zeroes at the stop. Then verify and park."""
        self._simulated_fault = False
        restore_alarm_detection: int | None = None
        home_failed = False

        try:
            self._emit_init("homing")
            await self.clear_alarm()

            try:
                # Pr0.03 = dr: the Pr8.10 direction bit is relative to it.
                await self.move_config()
                restore_alarm_detection = await self.get_param("Alarm detection")
                await self._configure_drive_homing(restore_alarm_detection)
            except Exception as e:
                raise MotorConfigError(f"Failed to configure drive homing: {str(e)}") from e

            try:
                await self._trigger_drive_homing()
            except Exception as e:
                raise MotorError(f"Failed to start homing: {str(e)}") from e

            # A safety pause re-triggers homing from wherever the axis stopped.
            self._pending_motion = self._trigger_drive_homing

            t0 = time.monotonic()
            stop = await self.wait_for_move(timeout=MOTOR_HOME_TIMEOUT, homing=True)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if stop == MotorStatus.STOPPED:
                await self._verify_zero_position()
                await self._verify_home_limit_engaged()
                await self._park()

                self._is_homed = True
                self._hard_stopped = False
                self._emit_init("ready")
            elif stop == MotorStatus.FAULT:
                self._is_homed = False
                self._hard_stopped = True
                warning = await self._read_pr_warning()
                detail = "" if not warning else f" (PR warning 0x{warning:04X})"

                raise MotorError(f"Motor {self._motor_id} faulted during homing{detail}")
            else:
                self._is_homed = False

                raise MotorTimeoutError(f"Motor {self._motor_id} homing timed out")

        except Exception as e:
            home_failed = True
            if isinstance(e, MotorTimeoutError):
                self._is_homed = False
            reason = "timeout" if isinstance(e, MotorTimeoutError) else str(e)
            self._emit_init("failed", detail="home", reason=reason)
            if not isinstance(e, (MotorError, MotorConfigError, MotorTimeoutError)):
                raise MotorError(f"Unexpected error during homing: {str(e)}") from e
            raise
        finally:
            self._clear_pending_motion()
            if restore_alarm_detection is not None:
                try:
                    await self._write_verified({"Alarm detection": restore_alarm_detection})
                except Exception as restore_error:
                    self._is_homed = False
                    self._hard_stopped = True
                    message = f"Failed to restore motor {self._motor_id} alarm detection: {restore_error}"
                    if home_failed is True:
                        # Keep the homing failure as the reported cause.
                        self._logger.warning(message, extra={"motor_id": self._motor_id})
                    else:
                        self._emit_init("failed", detail="home", reason="homing override restore failed")
                        raise MotorConfigError(message)
