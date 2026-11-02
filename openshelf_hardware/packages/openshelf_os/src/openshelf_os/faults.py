"""Fault codes the robot reports for a failed action, and their operator sentences.

Route error bodies and the worker's failure events share this vocabulary, so a drive
alarm reads the same whether a route or a queued command hit it. The exception text
never becomes operator copy: it travels as `exception_debug`, which the server hands
only to the developer user.
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from hardware.exceptions import ModbusConnectionError
from hardware.exceptions import MotorConfigError as HardwareMotorConfigError
from hardware.exceptions import MotorTimeoutError as HardwareMotorTimeoutError
from openshelf_os.config.exceptions import ConfigError
from openshelf_os.exceptions import (
    GRIP_DROPPED_CODE,
    GRIP_IO_BOX_FAILED_CODE,
    GRIP_PICK_FAILED_CODE,
    GRIP_RELEASE_FAILED_CODE,
    ConfigError as RobotConfigError,
    GripVerificationError,
    MotorWaitTimeoutError,
    RotationClearanceError,
)

EXECUTION_ERROR_CODE = "execution_error"
WORKER_ERROR_CODE = "worker_error"

FAULT_MESSAGES: dict[str, str] = {
    "config_error": "The machine's settings are not valid. Check the settings.",
    EXECUTION_ERROR_CODE: "The robot could not finish the command. Check the robot before continuing.",
    GRIP_DROPPED_CODE: "The gripper lost the bin. Check where it fell, then recover the tool.",
    GRIP_IO_BOX_FAILED_CODE: "The gripper could not confirm the bin in the IO box. Check the IO box, then recover the tool.",
    GRIP_PICK_FAILED_CODE: "The gripper could not confirm it picked the bin. Check the shelf, then recover the tool.",
    GRIP_RELEASE_FAILED_CODE: "The gripper did not release the bin. Check the tool before continuing.",
    "motor_alarm": "A motor drive raised an alarm and stopped. Clear the alarm, then reset the robot.",
    "motor_bus_disconnected": "The robot's motors are not connected. Check the motor cable and power.",
    "motor_fault": "The robot reported a motor fault. Check the robot before continuing.",
    "motor_stalled": "A motor stalled before reaching its target. Check for an obstruction, then reset the robot.",
    "motor_timeout": "A motor did not reach its target in time. Check for an obstruction, then reset the robot.",
    "rotation_blocked": "The robot could not turn the tray or bin. Clear the way and try again.",
    WORKER_ERROR_CODE: "The robot stopped the command after an internal error. Check the robot before continuing.",
}

_CHAIN_LIMIT = 5
_TEXT_LIMIT = 500


def exception_chain(exc: Optional[BaseException]) -> Iterator[BaseException]:
    """Yield exc and its explicit causes, outermost first. Implicit context and
    exception groups are skipped: below the raise site they are framework wrappers
    (Starlette re-raises a middleware error from its task group)."""
    for _ in range(_CHAIN_LIMIT):
        if exc is None or isinstance(exc, BaseExceptionGroup):
            return
        yield exc
        exc = exc.__cause__


def fault_code(exc: BaseException) -> Optional[str]:
    """The most specific fault code the exception chain supports, if any."""
    chain = list(exception_chain(exc))
    # Typed causes first: a move's fault_context rides on the outer MotorError even
    # when the cause underneath is a bus disconnect or timeout.
    for current in chain:
        if isinstance(current, GripVerificationError):
            return current.failure_code
        if isinstance(current, RotationClearanceError):
            return "rotation_blocked"
        if isinstance(current, ModbusConnectionError):
            return "motor_bus_disconnected"
        if isinstance(current, (HardwareMotorTimeoutError, MotorWaitTimeoutError)):
            return "motor_timeout"
        if isinstance(current, (HardwareMotorConfigError, ConfigError, RobotConfigError)):
            return "config_error"
    for current in chain:
        fault_context = getattr(current, "fault_context", None)
        if isinstance(fault_context, dict):
            if fault_context.get("alarm_code"):
                return "motor_alarm"
            if fault_context.get("stall_detected") or fault_context.get("no_progress"):
                return "motor_stalled"
            return "motor_fault"
    return None


def exception_debug(exc: BaseException) -> dict[str, Any]:
    """Developer-facing description of an exception: its chain and motor fault context."""
    chain: list[str] = []
    debug: dict[str, Any] = {"chain": chain}
    for current in exception_chain(exc):
        text = str(current)[:_TEXT_LIMIT]
        chain.append(f"{type(current).__name__}: {text}" if text else type(current).__name__)
        fault_context = getattr(current, "fault_context", None)
        if isinstance(fault_context, dict) and "fault_context" not in debug:
            debug["fault_context"] = {
                str(key): value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
                for key, value in fault_context.items()
            }
    return debug
