import time
import asyncio
import functools
import logging

from dataclasses import dataclass
from hardware import motor_helpers
from hardware.controller import SharedModbusController
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Callable, Awaitable

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
HOME_APPROACH_TOLERANCE_MM = 1.0
DEFAULT_HOME_APPROACH_BACKOFF_MM = 5.0
DEFAULT_BRAKE_OUTPUT_FUNCTION = 0xA1
RELEASED_BRAKE_OUTPUT_FUNCTION = 0x24

logger = logging.getLogger(__name__)


class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3
    FAULT = 4
    TIMEOUT = 5


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
        self._stall_timeout = 2.0
        self._status = MotorStatus.IDLE
        self._pending_motion: Optional[Callable[[], Awaitable[None]]] = None
        self._target_pos: Optional[float] = None
        self._last_bad_position_log_at = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False
        self._on_move_complete: Optional[Callable[[float], None]] = None
        self._is_homed = False
        self._hard_stopped = False
        self._should_skip_pre_home_once = False
        self._configured_brake_output_function = int(
            getattr(self._params, "brake_output_function", DEFAULT_BRAKE_OUTPUT_FUNCTION)
        )
        self._active_brake_output_function = self._configured_brake_output_function

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    async def configure(self):
        try:
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
            await self.set_param("SO2 output function", self._configured_brake_output_function)
            self._active_brake_output_function = self._configured_brake_output_function
            await self._controller.write_single_register(self._motor_id, 0x0167, 100)
            await self._controller.write_single_register(self._motor_id, 0x0169, 100)
            await self._controller.write_single_register(self._motor_id, 0x016B, 10)
            max_following_error = getattr(self._params, "max_following_error", 4000)
            await self.set_param("Allowed max position following error", max_following_error)
            await self.set_param("Position loop Kp", 50)
            await self.set_param("Max stop time", 100)
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)
            await self.set_param("Peak current", self._params.Ip)

            # Configure movement parameters
            await self.move_config()
        except Exception as e:
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

    async def get_param(self, register: str) -> Optional[int]:
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

    async def get_position(self) -> float:
        try:
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

                    return fallback_pos

            self._cur_pos = logical_pos

            return logical_pos

        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to get position for motor {self._motor_id}: {str(e)}") from e
            raise

    # ------------------------------------------------------------------ #
    #  Brake management
    # ------------------------------------------------------------------ #

    async def set_brake_output_function(self, value: int, *, force: bool = False) -> None:
        brake_output_function = int(value)

        if force is False and self._active_brake_output_function == brake_output_function:
            return

        await self.set_param("SO2 output function", brake_output_function)
        self._active_brake_output_function = brake_output_function

    async def disable_brake_output(self) -> None:
        await self.set_brake_output_function(RELEASED_BRAKE_OUTPUT_FUNCTION)

    async def restore_brake_output(self) -> None:
        await self.set_brake_output_function(self._configured_brake_output_function, force=True)

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

    async def _start_absolute_move(self, value: float, rpm: Optional[int] = None, accel: Optional[int] = None) -> None:
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

        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x10
        )

        await asyncio.sleep(0.1)

        self._status = MotorStatus.MOVING
        self._pending_motion = functools.partial(self._start_absolute_move, value, rpm, accel)

        self._logger.debug(
            "Started absolute move",
            extra={
                "motor_id": self._motor_id,
                "target_mm": value,
                "limit_mm": self._params.lim,
                "rpm": rpm or self._params.rpm,
                "accel": accel or self._params.accel,
            }
        )

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
            await self._start_absolute_move(value, rpm, accel)
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

    async def move_to_position_no_wait(self, value: float, rpm: Optional[int] = None, accel: Optional[int] = None) -> None:
        try:
            if self._safety:
                if self._safety.blocked:
                    self._logger.debug(
                        "Safety guard active before move; waiting to clear",
                        extra={"motor_id": self._motor_id, "target_mm": value},
                    )
                await self._safety.wait_clear()

            await self._start_absolute_move(value, rpm, accel)

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

        try:
            while True:
                if self._is_move_cancelled(should_abort) is True:
                    await self._cancel_motion(homing=homing)
                    did_cancel_motion = True
                    raise asyncio.CancelledError()

                snapshot = await self._read_motion_snapshot()
                await self._update_motion_progress(state)

                if await self._pause_for_safety_if_needed(state):
                    continue

                if snapshot.is_complete and self._is_completion_stable(state):
                    return await self._finish_completed_move(homing, state)

                alarm_state = await self._read_alarm_state(homing, state)

                if self._is_homing_limit_stop(homing, alarm_state, state):
                    return await self._finish_homing_limit_stop(snapshot, alarm_state, state)

                fault_state = self._detect_motion_fault(snapshot, alarm_state, state)

                if fault_state.is_fault:
                    return await self._finish_fault(homing, snapshot, alarm_state, fault_state, state)

                if time.monotonic() - state.start_time > timeout:
                    return await self._finish_timeout(timeout, homing, state)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            if homing is True and did_cancel_motion is False:
                await self._cancel_motion(homing=True)
            raise

    def _is_completion_stable(self, state: MotionWaitState) -> bool:
        return state.has_moved or time.monotonic() - state.start_time > 0.5

    def _clear_pending_motion(self) -> None:
        self._pending_motion = None
        self._target_pos = None

    async def _stop_and_clear_motion(self) -> None:
        await self.stop()
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

    async def _update_motion_progress(self, state: MotionWaitState) -> None:
        await self.get_position()
        now = time.monotonic()

        if state.last_pos is not None:
            if self._cur_pos != state.last_pos:
                state.last_movement_time = now
                state.has_moved = True

        state.last_pos = self._cur_pos

    async def _pause_for_safety_if_needed(self, state: MotionWaitState) -> bool:
        if not self._safety or not self._safety.blocked:
            return False

        pause_started = time.monotonic()

        self._logger.info(
            "Safety guard stop",
            extra={"motor_id": self._motor_id, "position_mm": self._cur_pos},
        )

        await self.stop()
        await self._safety.wait_clear()

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

        if self._target_pos is not None:
            try:
                current_pos = await self.get_position()
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
                    self._hard_stopped = True
                    self._is_homed = False

                    return MotorStatus.FAULT
            except Exception as position_exc:
                self._logger.warning(
                    "Failed to verify motor position after move",
                    extra={"motor_id": self._motor_id, "error": str(position_exc)},
                )

        self._target_pos = None
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

        self._logger.warning(
            "Limit stop detected during homing",
            extra={
                "motor_id": self._motor_id,
                "elapsed": time.monotonic() - state.start_time,
                "status_word": snapshot.status_word,
                "alarm_code": alarm_state.alarm_code,
            },
        )

        return MotorStatus.STOPPED

    def _detect_motion_fault(
        self,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        state: MotionWaitState,
    ) -> MotionFaultState:
        now = time.monotonic()
        no_progress = (
            not state.has_moved and (now - state.start_time > max(2.0, self._stall_timeout * 6))
        )
        stall_detected = (
            state.has_moved and (now - state.last_movement_time > self._stall_timeout)
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

    async def _finish_fault(
        self,
        homing: bool,
        snapshot: MotionSnapshot,
        alarm_state: MotionAlarmState,
        fault_state: MotionFaultState,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self.stop()

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
                },
            )

            self._hard_stopped = True
            self._is_homed = False

            return MotorStatus.FAULT

        fault_type = "Simulated fault" if self._simulated_fault else "Fault"
        self._target_pos = None

        self._logger.warning(
            f"{fault_type} detected - stopping motor, manual intervention required",
            extra={
                "motor_id": self._motor_id,
                "homing": homing,
                "status_word": snapshot.status_word,
                "position_mm": self._cur_pos,
                "alarm_code": alarm_state.alarm_code,
                "stall_detected": fault_state.stall_detected,
                "no_progress": fault_state.no_progress,
                "simulated": self._simulated_fault,
            },
        )

        self._pending_motion = None
        self._hard_stopped = True
        self._is_homed = False

        return MotorStatus.FAULT

    async def _finish_timeout(
        self,
        timeout: float,
        homing: bool,
        state: MotionWaitState,
    ) -> MotorStatus:
        await self._stop_and_clear_motion()

        self._logger.error(
            "Motion timeout",
            extra={
                "motor_id": self._motor_id,
                "timeout": timeout,
                "homing": homing,
                "elapsed": time.monotonic() - state.start_time,
            }
        )

        return MotorStatus.TIMEOUT

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

    # ------------------------------------------------------------------ #
    #  Public homing API
    # ------------------------------------------------------------------ #

    async def home(self) -> None:
        self._simulated_fault = False

        try:
            await self.clear_alarm()

            home_dir = self._get_home_direction()
            home_rpm = self._params.home_rpm or self._params.ret_rpm

            try:
                await self._move_near_home(self._params.ret_rpm)
            except Exception as e:
                raise MotorConfigError(f"Failed during pre-home approach: {str(e)}") from e

            try:
                await self._prepare_home_motion(home_rpm, home_dir)
            except Exception as e:
                raise MotorConfigError(f"Failed to configure homing parameters: {str(e)}") from e

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
            if not isinstance(e, (MotorError, MotorConfigError, MotorTimeoutError)):
                raise MotorError(f"Unexpected error during homing: {str(e)}") from e
            raise
