"""Fault codes the robot reports for a failed action, and their operator sentences.

Route error bodies and the worker's failure events share this vocabulary, so a drive
alarm reads the same whether a route or a queued command hit it. The exception text
never becomes operator copy: it goes to the log under the request's correlation id.
"""
from __future__ import annotations

from typing import Iterator, Optional

from hardware.exceptions import ModbusConnectionError
from hardware.exceptions import MotorConfigError as HardwareMotorConfigError
from hardware.exceptions import MotorSafetyStopError
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
RETRIEVE_FAILED_CODE = "retrieve_failed"
STORE_FAILED_CODE = "store_failed"
WORKER_ERROR_CODE = "worker_error"

FAULT_MESSAGES: dict[str, str] = {
    "config_error": "The machine's settings are not valid. Check the settings.",
    EXECUTION_ERROR_CODE: "The robot could not finish the command. Check the robot before continuing.",
    GRIP_DROPPED_CODE: "The gripper lost the bin. Check where it fell, then recover the tool.",
    GRIP_IO_BOX_FAILED_CODE: "The gripper could not confirm the bin in the IO box. Check the IO box, then recover the tool.",
    GRIP_PICK_FAILED_CODE: "The gripper could not confirm it picked the bin. Check the shelf, then recover the tool.",
    GRIP_RELEASE_FAILED_CODE: "The gripper did not release the bin. Check the tool before continuing.",
    "motor_alarm": "A motor drive raised an alarm and stopped. Clear the alarm, then reset the robot.",
    "motor_bus_disconnected": "A device on the machine bus is not connected. Check its cable and power.",
    "motor_fault": "The robot reported a motor fault. Check the robot before continuing.",
    "motor_stalled": "A motor stalled before reaching its target. Check for an obstruction, then reset the robot.",
    "motor_timeout": "A motor did not reach its target in time. Check for an obstruction, then reset the robot.",
    RETRIEVE_FAILED_CODE: "The robot could not retrieve the item. Check the robot before continuing.",
    "rotation_blocked": "The robot could not turn the tray or bin. Clear the way and try again.",
    "safety_stopped": "A safety guard stopped the robot before it finished. Clear it, then reset the robot.",
    STORE_FAILED_CODE: "The robot could not store the item. Check the robot before continuing.",
    WORKER_ERROR_CODE: "The robot stopped the command after an internal error. Check the robot before continuing.",
}

_CHAIN_LIMIT = 5


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
    """The fault code of the root cause in the exception chain, if any.

    The chain is walked innermost first and the first typed cause wins: grip
    verification, rotation refusal, bus loss, motor timeout, or an element carrying a
    motor fault_context (alarm, stall, other fault). Wrappers such as MotorConfigError
    (raised around homing and configure) report config_error only when nothing else
    in the chain is typed.
    """
    config = False
    for current in reversed(list(exception_chain(exc))):
        if isinstance(current, MotorSafetyStopError):
            return "safety_stopped"
        if isinstance(current, GripVerificationError):
            return current.failure_code
        if isinstance(current, RotationClearanceError):
            return "rotation_blocked"
        if isinstance(current, ModbusConnectionError):
            return "motor_bus_disconnected"
        if isinstance(current, (HardwareMotorTimeoutError, MotorWaitTimeoutError)):
            return "motor_timeout"
        fault_context = getattr(current, "fault_context", None)
        if isinstance(fault_context, dict):
            if fault_context.get("alarm_code"):
                return "motor_alarm"
            if fault_context.get("stall_detected") or fault_context.get("no_progress"):
                return "motor_stalled"
            return "motor_fault"
        if isinstance(current, (HardwareMotorConfigError, ConfigError, RobotConfigError)):
            config = True
    return "config_error" if config else None


def fault_failure(exc: BaseException, fallback_code: str) -> tuple[str, str]:
    """(fault code, operator sentence) for a failed action: the classified cause in the
    chain, else `fallback_code`, which must be a FAULT_MESSAGES key."""
    code = fault_code(exc) or fallback_code
    return code, FAULT_MESSAGES[code]
