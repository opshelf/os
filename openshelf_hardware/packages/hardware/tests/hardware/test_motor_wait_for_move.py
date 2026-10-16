from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.motor import Motor, MotorStatus, MotionAlarmState, MotionSnapshot, MotionWaitState
from hardware.exceptions import MotorError


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
async def test_wait_for_move_preserves_fault_raised_during_completion(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor._target_pos = 1.0
    motor._is_homed = True
    motor.get_param = AsyncMock(return_value=0)
    position_reads = 0

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1
        motor._cur_pos = 0.0 if position_reads == 1 else 1.0
        if position_reads == 3:
            motor.set_fault()
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=did_get_position)
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    mock_controller.write_single_register.assert_awaited_once_with(1, 0x6002, 0x40)
    assert status is MotorStatus.FAULT
    assert motor._status is MotorStatus.FAULT
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False


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
async def test_wait_for_move_position_error_still_reads_alarm_and_invalidates_motion(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._read_motion_snapshot = AsyncMock(
        return_value=MotionSnapshot(status_word=0x40, is_complete=False, has_fault_bit=False)
    )
    motor.get_position = AsyncMock(side_effect=MotorError("position read failed"))
    motor.get_param = AsyncMock(return_value=17)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    motor._cur_pos = 12.0
    motor._is_homed = True

    with pytest.raises(MotorError, match="position read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    motor.get_param.assert_awaited_once_with("Current alarm")
    motor.stop.assert_awaited_once()
    assert motor._status is MotorStatus.ERROR
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._cur_pos is None
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True
    assert exc_info.value.fault_context == {
        "motor_id": 1,
        "status_word": 0x40,
        "alarm_code": 17,
        "stall_detected": None,
        "no_progress": None,
        "safety_blocked": False,
        "stop_error": None,
        "flight_recorder_path": "data/motion_faults/y.json",
        "position_known": False,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_wait_for_move_does_not_resume_when_terminal_status_arrives_during_safety_stop(
    mock_controller,
    terminal_status,
):
    safety = _BlockingSafety()
    motor = Motor(mock_controller, make_motor_params(), safety=safety)
    motor.get_param = AsyncMock(return_value=0)
    motor.get_position = mock_position_reader(motor, [0.0])
    pending_motion = AsyncMock()
    motor._pending_motion = pending_motion
    motor._target_pos = 50.0
    motor._is_homed = True
    mock_controller.read_holding_registers.return_value = 0

    async def stop_with_terminal_status():
        motor._status = terminal_status
        return terminal_status

    motor.stop = AsyncMock(side_effect=stop_with_terminal_status)

    with pytest.raises(MotorError, match="entered terminal state while paused for safety"):
        await motor.wait_for_move(timeout=0.5)

    assert motor.stop.await_count == 2
    safety.wait_clear.assert_not_awaited()
    pending_motion.assert_not_awaited()
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._status is terminal_status
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False


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


@pytest.mark.asyncio
async def test_flight_recorder_samples_brake_output_on_braked_axis(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis", brake_output_function=0x24))
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = mock_position_reader(motor, [0.0, 1.0])
    reads = {"Current alarm": 0, "Digital output state": 0b10}
    motor.get_param = AsyncMock(side_effect=lambda name: reads[name])
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.STOPPED
    assert [entry.digital_output_state for entry in motor._flight_recorder] == [0b10]


@pytest.mark.asyncio
async def test_flight_recorder_brake_output_read_failure_does_not_fail_move(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis", brake_output_function=0x24))
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = mock_position_reader(motor, [0.0, 1.0])

    async def did_get_param(name):
        if name == "Digital output state":
            raise MotorError("bus timeout")
        return 0

    motor.get_param = AsyncMock(side_effect=did_get_param)
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.STOPPED
    assert [entry.digital_output_state for entry in motor._flight_recorder] == [None]


@pytest.mark.asyncio
async def test_flight_recorder_skips_brake_output_read_on_unbraked_axis(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="x_axis"))
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = mock_position_reader(motor, [0.0, 1.0])
    motor.get_param = AsyncMock(return_value=0)
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    await motor.wait_for_move(timeout=0.5)

    assert all(call.args[0] != "Digital output state" for call in motor.get_param.await_args_list)
    assert [entry.digital_output_state for entry in motor._flight_recorder] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(("position_mm", "is_immediate"), [(49.94, True), (45.0, False)])
async def test_wait_for_move_trusts_completion_only_on_target_without_observed_motion(
    mock_controller, monkeypatch, position_mm: float, is_immediate: bool
):
    """An axis that already stopped on its target needs no 0.5s guard; one that stands elsewhere does."""
    clock = {"now": 100.0}
    monkeypatch.setattr("hardware.motor.time.monotonic", lambda: clock["now"])

    async def tick(_seconds: float) -> None:
        clock["now"] += 0.05

    monkeypatch.setattr("hardware.motor.asyncio.sleep", tick)
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor._target_pos = 50.0

    async def did_get_position():
        motor._cur_pos = position_mm
        return position_mm

    motor.get_position = AsyncMock(side_effect=did_get_position)
    mock_controller.read_holding_registers.return_value = 1 << 5

    await motor.wait_for_move(timeout=5.0)

    waited_s = clock["now"] - 100.0
    assert (waited_s < 0.1) is is_immediate


@pytest.mark.asyncio
async def test_homing_wait_does_not_trust_completion_on_a_stale_target(mock_controller, monkeypatch):
    """Homing has no absolute target, so a leftover target must not end the wait on a stale complete bit."""
    clock = {"now": 100.0}
    monkeypatch.setattr("hardware.motor.time.monotonic", lambda: clock["now"])

    async def tick(_seconds: float) -> None:
        clock["now"] += 0.05

    monkeypatch.setattr("hardware.motor.asyncio.sleep", tick)
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor._target_pos = 50.0

    async def did_get_position():
        motor._cur_pos = 50.0
        return 50.0

    motor.get_position = AsyncMock(side_effect=did_get_position)
    mock_controller.read_holding_registers.return_value = 1 << 5

    await motor.wait_for_move(timeout=5.0, homing=True)

    assert clock["now"] - 100.0 > 0.5


@pytest.mark.asyncio
async def test_jog_clears_a_stale_target(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="z_axis", logical_to_physical_delta=lambda value: value),
    )
    seen = []

    async def fake_wait_for_move(*args, **kwargs):
        seen.append(motor._target_pos)
        motor._status = MotorStatus.STOPPED

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor.move_config = AsyncMock()
    motor._target_pos = 50.0

    await motor.jog(1.0)

    assert seen == [None]


@pytest.mark.asyncio
async def test_start_path_zero_clears_a_stale_target(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="z_axis"))
    motor._verify_brake_output_before_motion_trigger = AsyncMock()
    motor._enable_for_motion = AsyncMock()
    motor._target_pos = 50.0

    await motor._start_path_zero()

    assert motor._target_pos is None
    assert motor._pending_motion is None
