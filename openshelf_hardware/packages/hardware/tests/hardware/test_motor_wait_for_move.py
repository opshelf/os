from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.motor import Motor, MotorStatus


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
