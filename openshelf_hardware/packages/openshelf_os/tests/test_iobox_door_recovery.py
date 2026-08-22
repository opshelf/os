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
async def test_home_door_caches_closed_when_post_home_read_is_invalid() -> None:
    iobox = make_iobox()
    iobox._door_open = True
    iobox._door_candidate_state = True
    iobox._door_candidate_since = 1.0
    iobox._md.get_position.return_value = None

    await iobox.home_door()

    assert iobox.get_door_state() is False
    assert iobox._door_candidate_state is None
    assert iobox._door_candidate_since is None
    iobox._md.home.assert_awaited_once()
    iobox._md.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_home_door_keeps_closed_cache_when_post_home_read_looks_open() -> None:
    iobox = make_iobox()
    iobox._door_open = True
    iobox._door_candidate_state = True
    iobox._door_candidate_since = 1.0
    iobox._md.get_position.return_value = 65.0

    await iobox.home_door()

    assert iobox.get_door_state() is False
    assert iobox._door_candidate_state is None
    assert iobox._door_candidate_since is None
    iobox._md.home.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_door_readings_observe_refresh_throttle() -> None:
    iobox = make_iobox()
    iobox._md.get_position.return_value = -1.0

    await iobox.refresh_door_state()
    await iobox.refresh_door_state()

    iobox._md.get_position.assert_awaited_once()
    assert iobox._door_bad_reading_count == 1


@pytest.mark.asyncio
async def test_none_door_reading_observes_refresh_throttle_unless_forced() -> None:
    iobox = make_iobox()
    iobox._md.get_position.return_value = None

    await iobox.refresh_door_state()
    await iobox.refresh_door_state()
    await iobox.refresh_door_state(force=True)

    assert iobox._door_last_read_at is not None
    assert iobox._md.get_position.await_count == 2


@pytest.mark.asyncio
async def test_exceptional_door_reading_observes_refresh_throttle_unless_forced() -> None:
    iobox = make_iobox()
    iobox._md.get_position.side_effect = RuntimeError("Modbus read failed")

    await iobox.refresh_door_state()
    await iobox.refresh_door_state()
    await iobox.refresh_door_state(force=True)

    assert iobox._door_last_read_at is not None
    assert iobox._md.get_position.await_count == 2


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
async def test_bad_reading_recovery_caches_closed_after_successful_home() -> None:
    iobox = make_iobox()
    iobox._door_open = True
    iobox._door_candidate_state = True
    iobox._door_candidate_since = 1.0
    iobox._door_bad_reading_count = 4
    iobox._md.get_position.return_value = -1.0

    assert await iobox.refresh_door_state(force=True) is False

    iobox._md.home.assert_awaited_once()
    assert iobox._door_bad_reading_count == 0
    assert iobox._door_candidate_state is None
    assert iobox._door_candidate_since is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached_state", "revalidated_position_mm", "expected_state"),
    [
        (False, 65.0, True),
        (True, -0.01, False),
    ],
)
async def test_valid_locked_revalidation_updates_opposing_cached_door_state(
    cached_state: bool,
    revalidated_position_mm: float,
    expected_state: bool,
) -> None:
    iobox = make_iobox()
    iobox._door_open = cached_state
    iobox._door_bad_reading_count = 4
    iobox._md.get_position.side_effect = (-1.0, revalidated_position_mm)

    assert await iobox.refresh_door_state(force=True) is expected_state

    assert iobox._door_bad_reading_count == 0
    iobox._md.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_waits_for_bad_reading_recovery_home() -> None:
    iobox = make_iobox()
    iobox._door_bad_reading_count = 4
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    positions = iter((-1.0, -1.0, 65.0))

    async def get_position() -> float:
        return next(positions)

    async def home() -> None:
        recovery_started.set()
        await release_recovery.wait()

    iobox._md.get_position.side_effect = get_position
    iobox._md.home.side_effect = home

    refresh_task = asyncio.create_task(iobox.refresh_door_state(force=True))
    await recovery_started.wait()
    open_task = asyncio.create_task(iobox.open_door())
    await asyncio.sleep(0)

    iobox._md.move_to_position.assert_not_awaited()

    release_recovery.set()
    await refresh_task
    await open_task

    iobox._md.move_to_position.assert_awaited_once_with(65.0, rpm=None)


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
@pytest.mark.parametrize(
    ("position_mm", "is_valid"),
    [
        (1.0, True),
        (1.000001, False),
    ],
)
async def test_recovery_home_verification_boundary(
    position_mm: float,
    is_valid: bool,
) -> None:
    iobox = make_iobox()
    iobox._md.get_position.return_value = position_mm

    if is_valid:
        await iobox._verify_door_homed()
    else:
        with pytest.raises(HardwareMotorError, match="did not establish a valid zero position"):
            await iobox._verify_door_homed()

    iobox._md.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_alarm_retry_does_not_start_second_recovery_cycle() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = (fault, fault)
    iobox._md.get_position.return_value = 1.0

    with pytest.raises(HardwareMotorError, match="Movement failed"):
        await iobox._move_door_with_retry(65.0, operation="open")

    assert iobox._md.move_to_position.await_count == 2
    iobox._md.home.assert_awaited_once()
    iobox._md.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_alarm_recovery_home_caches_closed_before_retry() -> None:
    iobox = make_iobox()
    iobox._door_open = True
    iobox._door_candidate_state = True
    iobox._door_candidate_since = 1.0
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = (fault, None)
    iobox._md.get_position.return_value = 0.09

    await iobox._move_door_with_retry(65.0, operation="open")

    assert iobox.get_door_state() is False
    assert iobox._door_candidate_state is None
    assert iobox._door_candidate_since is None
    iobox._md.home.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_uses_alarm_register_only_with_explicit_safe_fault_context() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": None,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = (fault, None)
    iobox._md.get_param.return_value = 32
    iobox._md.get_position.side_effect = (0.09, 65.0)

    await iobox.open_door()

    assert iobox._md.move_to_position.await_count == 2
    iobox._md.get_param.assert_awaited_once_with("Current alarm")
    iobox._md.home.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_does_not_retry_when_alarm_code_is_missing_from_fault_context() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = fault
    iobox._md.get_param.return_value = 32

    with pytest.raises(MotorError, match="Failed to open door"):
        await iobox.open_door()

    iobox._md.move_to_position.assert_awaited_once()
    iobox._md.get_param.assert_not_awaited()
    iobox._md.home.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("disqualifier", ["stall_detected", "no_progress", "safety_blocked"])
async def test_open_does_not_retry_alarm_32_with_explicit_disqualifier(
    disqualifier: str,
) -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    fault.fault_context[disqualifier] = True
    iobox._md.move_to_position.side_effect = fault

    with pytest.raises(MotorError, match="Failed to open door"):
        await iobox.open_door()

    iobox._md.move_to_position.assert_awaited_once()
    iobox._md.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_does_not_retry_incomplete_generic_fault_context() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=ERROR)")
    fault.fault_context = {
        "alarm_code": 32,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = fault

    with pytest.raises(MotorError, match="Failed to open door"):
        await iobox.open_door()

    iobox._md.move_to_position.assert_awaited_once()
    iobox._md.get_param.assert_not_awaited()
    iobox._md.home.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_open_does_not_retry_when_shared_safety_state_is_missing() -> None:
    iobox = make_iobox()
    iobox._md._safety = None
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


@pytest.mark.asyncio
async def test_open_does_not_retry_when_safety_blocks_during_alarm_register_read() -> None:
    iobox = make_iobox()
    fault = HardwareMotorError("Movement failed for motor 31 (status=FAULT)")
    fault.fault_context = {
        "alarm_code": None,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
    }
    iobox._md.move_to_position.side_effect = fault

    async def get_current_alarm() -> int:
        iobox._md._safety.blocked = True
        return 32

    iobox._md.get_param.side_effect = get_current_alarm

    with pytest.raises(MotorError, match="Failed to open door"):
        await iobox.open_door()

    iobox._md.move_to_position.assert_awaited_once()
    iobox._md.get_param.assert_awaited_once_with("Current alarm")
    iobox._md.home.assert_not_awaited()
