from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.motor import Motor, MotorStatus, MotionAlarmState, MotionFaultState, MotionSnapshot, MotionWaitState


def make_motor_params(**overrides):
    params = {
        "motor_id": 1,
        "lim": 100.0,
        "home_rpm": 100,
        "ret_rpm": 200,
        "rpm": 300,
        "accel": 100,
        "Ip": 10,
        "dr": 0,
        "mpr": 80.0,
        "phys_at_zero": 0.0,
        "phys_at_max": 100.0,
        "status_reg": 0x1003,
        "status_fault_bit": 0,
        "status_complete_bit": 5,
        "ppr": 10000,
        "max_following_error": 4000,
    }
    params.update(overrides)
    return SimpleNamespace(**params)


def mock_position_reader(motor, values):
    positions = iter(values)

    async def did_get_position():
        motor._cur_pos = next(positions)
        return motor._cur_pos

    return AsyncMock(side_effect=did_get_position)


@pytest.mark.asyncio
async def test_wait_for_move_completes_after_observed_motion(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.get_position = mock_position_reader(motor, [0.0, 1.0])
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.STOPPED
    motor.stop.assert_awaited_once()
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is False


@pytest.mark.asyncio
async def test_wait_for_move_times_out_and_clears_motion_state(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = mock_position_reader(motor, [0.0])
    motor.get_param = AsyncMock(return_value=0)
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    mock_controller.read_holding_registers.return_value = 0

    status = await motor.wait_for_move(timeout=-1.0)

    assert status == MotorStatus.TIMEOUT
    motor.stop.assert_awaited_once()
    assert motor._pending_motion is None
    assert motor._target_pos is None


@pytest.mark.asyncio
async def test_wait_for_move_faults_when_completion_is_away_from_target(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.get_position = mock_position_reader(motor, [0.0, 10.0, 10.0])
    motor._target_pos = 50.0
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.FAULT
    motor.stop.assert_awaited_once()
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_fresh_progress_read_failure_returns_false(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params())
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0, last_pos=1.0)
    motor.get_position = AsyncMock(side_effect=RuntimeError("read failed"))
    monkeypatch.setattr("hardware.motor.asyncio.sleep", AsyncMock())

    did_observe_progress = await motor._did_observe_progress_on_fresh_read(state)

    assert did_observe_progress is False


@pytest.mark.asyncio
async def test_wait_for_move_faults_when_fresh_progress_read_fails(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor._target_pos = 50.0
    motor._pending_motion = AsyncMock()
    motor._dump_flight_recorder = lambda **_: None
    mock_controller.read_holding_registers.return_value = 0
    monkeypatch.setattr("hardware.motor.asyncio.sleep", AsyncMock())

    position_reads = 0

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1

        if position_reads == 1:
            motor._cur_pos = 0.0
            return motor._cur_pos

        raise RuntimeError("fresh read failed")

    motor.get_position = AsyncMock(side_effect=did_get_position)
    motor._detect_motion_fault = lambda *_: MotionFaultState(
        is_fault=True,
        stall_detected=True,
        no_progress=False,
    )

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.FAULT
    assert motor.stop.await_count == 1
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is True


class _BlockingSafety:
    def __init__(self) -> None:
        self.blocked = True
        self.wait_clear = AsyncMock(side_effect=self.did_wait_clear)

    async def did_wait_clear(self) -> None:
        self.blocked = False


@pytest.mark.asyncio
async def test_wait_for_move_pauses_and_resumes_for_safety(mock_controller):
    safety = _BlockingSafety()
    motor = Motor(mock_controller, make_motor_params(), safety=safety)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.get_position = mock_position_reader(motor, [0.0, 0.0, 1.0])
    pending_motion = AsyncMock()
    motor._pending_motion = pending_motion
    mock_controller.read_holding_registers.side_effect = [0, 0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.STOPPED
    assert motor.stop.await_count == 2
    safety.wait_clear.assert_awaited_once()
    pending_motion.assert_awaited_once()


def test_motion_fault_ignores_stale_stall_when_current_sample_moved(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params())
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0, last_pos=990.0, has_moved=True)
    snapshot = MotionSnapshot(status_word=70, is_complete=False, has_fault_bit=False)
    alarm_state = MotionAlarmState(alarm_code=0, is_active=False)

    monkeypatch.setattr("hardware.motor.time.monotonic", lambda: 5.1)

    fault_state = motor._detect_motion_fault(
        snapshot,
        alarm_state,
        state,
        position_delta=10.0,
    )

    assert fault_state.is_fault is False
    assert fault_state.stall_detected is False


def test_motion_fault_detects_stall_when_no_progress_is_observed(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params())
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0, last_pos=990.0, has_moved=True)
    snapshot = MotionSnapshot(status_word=70, is_complete=False, has_fault_bit=False)
    alarm_state = MotionAlarmState(alarm_code=0, is_active=False)

    monkeypatch.setattr("hardware.motor.time.monotonic", lambda: 5.1)

    fault_state = motor._detect_motion_fault(
        snapshot,
        alarm_state,
        state,
        position_delta=0.0,
    )

    assert fault_state.is_fault is True
    assert fault_state.stall_detected is True
