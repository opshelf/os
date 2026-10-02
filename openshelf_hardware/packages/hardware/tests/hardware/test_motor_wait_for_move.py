from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.motor import Motor, MotorStatus, MotionAlarmState, MotionFaultState, MotionSnapshot, MotionWaitState
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
async def test_fresh_progress_read_failure_propagates_original_error(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params())
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0, last_pos=1.0)
    position_error = RuntimeError("read failed")
    motor.get_position = AsyncMock(side_effect=position_error)
    monkeypatch.setattr("hardware.motor.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="read failed") as exc_info:
        await motor._did_observe_progress_on_fresh_read(state)

    assert exc_info.value is position_error


@pytest.mark.asyncio
async def test_wait_for_move_recovers_from_fresh_progress_read_failure(mock_controller, monkeypatch):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(side_effect=[0, 17])
    motor._target_pos = 50.0
    motor._pending_motion = AsyncMock()
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")
    motor._cur_pos = 12.0
    motor._is_homed = True
    mock_controller.read_holding_registers.return_value = 0
    monkeypatch.setattr("hardware.motor.asyncio.sleep", AsyncMock())

    position_reads = 0
    position_error = RuntimeError("fresh read failed")

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1

        if position_reads == 1:
            motor._cur_pos = 0.0
            return motor._cur_pos

        raise position_error

    motor.get_position = AsyncMock(side_effect=did_get_position)
    motor._detect_motion_fault = lambda *_: MotionFaultState(
        is_fault=True,
        stall_detected=True,
        no_progress=False,
    )

    with pytest.raises(RuntimeError, match="fresh read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    assert exc_info.value is position_error
    assert motor.get_param.await_args_list == [
        (("Current alarm",), {}),
        (("Current alarm",), {}),
    ]
    motor.stop.assert_awaited_once()
    assert motor._status is MotorStatus.ERROR
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._cur_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False
    assert exc_info.value.fault_context == {
        "motor_id": 1,
        "status_word": 0,
        "alarm_code": 17,
        "stall_detected": None,
        "no_progress": None,
        "safety_blocked": False,
        "stop_error": None,
        "flight_recorder_path": "data/motion_faults/y.json",
        "position_known": False,
    }


@pytest.mark.asyncio
async def test_wait_for_move_recovers_from_completion_position_read_failure(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(side_effect=[0, 17])
    motor._target_pos = 50.0
    motor._pending_motion = AsyncMock()
    motor._cur_pos = 12.0
    motor._is_homed = True
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")
    mock_controller.read_holding_registers.side_effect = [0, 1 << 5]

    position_error = MotorError("completion read failed")
    position_reads = 0

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1
        if position_reads == 1:
            motor._cur_pos = 0.0
            return motor._cur_pos
        if position_reads == 2:
            motor._cur_pos = 10.0
            return motor._cur_pos
        raise position_error

    motor.get_position = AsyncMock(side_effect=did_get_position)

    with pytest.raises(MotorError, match="completion read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    assert exc_info.value is position_error
    assert motor.get_param.await_args_list == [
        (("Current alarm",), {}),
        (("Current alarm",), {}),
    ]
    assert motor.stop.await_count == 2
    assert motor._status is MotorStatus.ERROR
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._cur_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False
    assert exc_info.value.fault_context == {
        "motor_id": 1,
        "status_word": 1 << 5,
        "alarm_code": 17,
        "stall_detected": None,
        "no_progress": None,
        "safety_blocked": False,
        "stop_error": None,
        "flight_recorder_path": "data/motion_faults/y.json",
        "position_known": False,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_wait_for_move_preserves_terminal_status_set_during_exception_stop(
    mock_controller,
    terminal_status,
):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._read_motion_snapshot = AsyncMock(
        return_value=MotionSnapshot(status_word=0x40, is_complete=False, has_fault_bit=False)
    )
    original_error = MotorError("position read failed")
    motor.get_position = AsyncMock(side_effect=original_error)
    motor.get_param = AsyncMock(return_value=17)
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    motor._cur_pos = 12.0
    motor._is_homed = True

    async def stop_with_terminal_status():
        motor._status = terminal_status
        return terminal_status

    motor.stop = AsyncMock(side_effect=stop_with_terminal_status)

    with pytest.raises(MotorError, match="position read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    assert exc_info.value is original_error
    motor.stop.assert_awaited_once()
    assert motor._status is terminal_status
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


@pytest.mark.asyncio
async def test_wait_for_move_position_error_refreshes_previously_read_alarm(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._read_motion_snapshot = AsyncMock(
        return_value=MotionSnapshot(status_word=0x40, is_complete=False, has_fault_bit=False)
    )
    position_reads = 0

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1
        if position_reads == 1:
            motor._cur_pos = 0.0
            return motor._cur_pos
        raise MotorError("position read failed")

    motor.get_position = AsyncMock(side_effect=did_get_position)
    motor.get_param = AsyncMock(side_effect=[0, 17])
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")

    with pytest.raises(MotorError, match="position read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    assert motor.get_param.await_args_list == [
        (("Current alarm",), {}),
        (("Current alarm",), {}),
    ]
    assert exc_info.value.fault_context["alarm_code"] == 17


@pytest.mark.asyncio
async def test_wait_for_move_marks_alarm_unknown_when_exception_refresh_fails(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._read_motion_snapshot = AsyncMock(
        return_value=MotionSnapshot(status_word=0x40, is_complete=False, has_fault_bit=False)
    )
    position_reads = 0
    position_error = MotorError("position read failed")
    alarm_error = RuntimeError("alarm refresh failed")

    async def did_get_position():
        nonlocal position_reads
        position_reads += 1
        if position_reads == 1:
            motor._cur_pos = 0.0
            return motor._cur_pos
        raise position_error

    motor.get_position = AsyncMock(side_effect=did_get_position)
    motor.get_param = AsyncMock(side_effect=[0, alarm_error])
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/y.json")

    with pytest.raises(MotorError, match="position read failed") as exc_info:
        await motor.wait_for_move(timeout=0.5)

    assert exc_info.value is position_error
    assert motor.get_param.await_args_list == [
        (("Current alarm",), {}),
        (("Current alarm",), {}),
    ]
    assert motor._flight_recorder[-1].alarm_code is None
    assert exc_info.value.fault_context["alarm_code"] is None
    assert exc_info.value.fault_context["alarm_read_error"] == "alarm refresh failed"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_wait_for_move_does_not_resume_when_terminal_status_arrives_while_safety_waits(
    mock_controller,
    terminal_status,
):
    safety = _BlockingSafety()
    motor = Motor(mock_controller, make_motor_params(), safety=safety)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.get_position = mock_position_reader(motor, [0.0])
    pending_motion = AsyncMock()
    motor._pending_motion = pending_motion
    motor._target_pos = 50.0
    motor._is_homed = True
    mock_controller.read_holding_registers.return_value = 0

    async def clear_safety_with_terminal_status():
        safety.blocked = False
        motor._status = terminal_status

    safety.wait_clear = AsyncMock(side_effect=clear_safety_with_terminal_status)

    with pytest.raises(MotorError, match="entered terminal state while waiting for safety to clear"):
        await motor.wait_for_move(timeout=0.5)

    assert motor.stop.await_count == 2
    safety.wait_clear.assert_awaited_once()
    pending_motion.assert_not_awaited()
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._status is terminal_status
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_homing_completion_returns_terminal_status_created_during_stop(
    mock_controller,
    terminal_status,
):
    motor = Motor(mock_controller, make_motor_params())
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0

    async def stop_with_terminal_status():
        motor._status = terminal_status
        return terminal_status

    motor.stop = AsyncMock(side_effect=stop_with_terminal_status)
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0)

    status = await motor._finish_completed_move(True, state)

    assert status is terminal_status
    assert motor._status is terminal_status
    assert motor._pending_motion is None
    assert motor._target_pos is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_finish_fault_preserves_terminal_status_created_during_stop(
    mock_controller,
    terminal_status,
):
    motor = Motor(mock_controller, make_motor_params())
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    motor._is_homed = True
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/fault.json")

    async def stop_with_terminal_status():
        motor._status = terminal_status
        return terminal_status

    motor.stop = AsyncMock(side_effect=stop_with_terminal_status)
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0)
    snapshot = MotionSnapshot(status_word=0x01, is_complete=False, has_fault_bit=True)
    alarm_state = MotionAlarmState(alarm_code=17, is_active=True)
    fault_state = MotionFaultState(is_fault=True, stall_detected=False, no_progress=False)

    status = await motor._finish_fault(False, snapshot, alarm_state, fault_state, state)

    assert status is terminal_status
    assert motor._status is terminal_status
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False
    assert motor._dump_flight_recorder.call_args.kwargs["reason"] == "fault"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT],
)
async def test_finish_timeout_preserves_terminal_status_created_during_stop(
    mock_controller,
    terminal_status,
):
    motor = Motor(mock_controller, make_motor_params())
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    motor._is_homed = True
    motor._dump_flight_recorder = Mock(return_value="data/motion_faults/timeout.json")

    async def stop_with_terminal_status():
        motor._status = terminal_status
        return terminal_status

    motor.stop = AsyncMock(side_effect=stop_with_terminal_status)
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0)

    status = await motor._finish_timeout(0.5, False, state)

    assert status is terminal_status
    assert motor._status is terminal_status
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False
    assert motor._dump_flight_recorder.call_args.kwargs["reason"] == "timeout"


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
