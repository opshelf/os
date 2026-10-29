"""Sentry #7681314056: a Modbus dropout during motor configuration used to reach Sentry as a
motor error.

The API process configures the axes over the Modbus bus. When a drive missed a round of
replies, the failure reached the operator's browser as an ERROR-level log with a traceback, and
the Sentry hooks in openshelf_os -- written for the pymodbus log records whose message is
nothing but "No response received after N retries, continue with next request" -- passed it
through, because the wrapper prefixed that text with the parameter it was writing. It has to be
dropped on the same path as those records, in every process that reports.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hardware.controller import ModbusTransientNoResponseError
from hardware.motor import Motor

from openshelf_os import _before_send_sentry_log, operating_mode

PYMODBUS_NO_RESPONSE = "No response received after 3 retries, continue with next request"


async def test_configuration_dropout_is_not_reported_to_sentry(caplog) -> None:
    operating_mode.set_sentry_reporting_enabled(True)

    controller = AsyncMock()
    controller.write_single_register = AsyncMock(
        side_effect=ModbusTransientNoResponseError(PYMODBUS_NO_RESPONSE)
    )
    motor = Motor(controller, SimpleNamespace(motor_id=2, motor_name="y_axis", lim=100.0))

    with caplog.at_level(logging.DEBUG, logger="hardware.motor.motor.2"):
        try:
            await motor.configure()
        except Exception as failure:
            captured = failure
        else:
            raise AssertionError("configure() was expected to fail")

    assert PYMODBUS_NO_RESPONSE in str(captured)

    # No ERROR-level record: the LoggingIntegration would turn one into an issue for every
    # dropout, and there is nothing to report.
    assert [record.levelno for record in caplog.records if record.levelno >= logging.ERROR] == []

    # And a report built from the failure text is dropped by the hardware process' hook, the
    # way the pymodbus log records are.
    assert _before_send_sentry_log({"body": str(captured)}, {}) is None
