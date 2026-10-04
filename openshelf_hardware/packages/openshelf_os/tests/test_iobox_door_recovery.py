from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hardware.exceptions import MotorError as HardwareMotorError

from openshelf_os.exceptions import MotorError
from openshelf_os.iobox import IOBox, IOBoxStatus


def make_iobox() -> IOBox:
    motor = AsyncMock()
    motor._safety = SimpleNamespace(blocked=False)
    motor.get_param.return_value = 0
    motor.get_position.return_value = 0.0

    iobox = IOBox.__new__(IOBox)
    iobox._md = motor
    iobox._params = SimpleNamespace(
        motors=SimpleNamespace(
            by_name=lambda name: SimpleNamespace(
                lim=65.0,
                lower_lim=2.0,
                ret_rpm=500,
            )
        )
    )
    iobox._status = IOBoxStatus.IDLE
    iobox._logger = logging.getLogger("test_iobox_door_recovery")
    iobox._curtain_state = False
    iobox._door_open = False
    iobox._door_last_read_at = None
    iobox._door_min_refresh_s = 0.2
    iobox._door_open_threshold_mm = 5.0
    iobox._door_debounce_s = 0.0
    iobox._door_candidate_state = None
    iobox._door_candidate_since = None
    iobox._door_force_refresh_on_block = False
    iobox._safety_guard_bypassed = False
    iobox._light_curtain_bypassed = False
    iobox._door_bad_reading_count = 0
    iobox._door_max_bad_readings = 5
    iobox._door_max_position_mm = 1000.0
    iobox._door_home_verify_tolerance_mm = 1.0
    iobox._door_zero_position_tolerance_mm = 0.01
    iobox._door_motion_lock = asyncio.Lock()
    return iobox


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position_mm", "expected_bad_reading_count"),
    [
        (-0.01, 0),
        (-0.010001, 1),
    ],
)
async def test_negative_door_jitter_boundary_controls_bad_reading_classification(
    position_mm: float,
    expected_bad_reading_count: int,
) -> None:
    iobox = make_iobox()
    iobox._md.get_position.return_value = position_mm

    assert await iobox.refresh_door_state(force=True) is False

    assert iobox._door_bad_reading_count == expected_bad_reading_count
    iobox._md.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_reading_recovery_is_deferred_during_door_motion() -> None:
    iobox = make_iobox()
    iobox._door_bad_reading_count = 4
    iobox._md.get_position.return_value = -1.0

    await iobox._door_motion_lock.acquire()
    try:
        await iobox.refresh_door_state(force=True)
    finally:
        iobox._door_motion_lock.release()

    iobox._md.home.assert_not_awaited()
    assert iobox._door_bad_reading_count == 5

    await iobox.refresh_door_state(force=True)

    iobox._md.home.assert_awaited_once()
    assert iobox._door_bad_reading_count == 0


@pytest.mark.asyncio
async def test_open_retries_once_after_recoverable_limit_alarm() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = (fault, None)
    iobox._md.get_position.side_effect = (0.09, 65.0)

    await iobox.open_door()

    assert iobox._md.move_to_position.await_count == 2
    iobox._md.get_param.assert_not_awaited()
    iobox._md.home.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_does_not_retry_when_shared_safety_blocks_after_fault() -> None:
    iobox = make_iobox()
    iobox._md._safety = SimpleNamespace(blocked=True)
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = fault

    with pytest.raises(MotorError, match="Failed to open door"):
        await iobox.open_door()

    iobox._md.move_to_position.assert_awaited_once()
    iobox._md.get_param.assert_not_awaited()
    iobox._md.home.assert_not_awaited()


