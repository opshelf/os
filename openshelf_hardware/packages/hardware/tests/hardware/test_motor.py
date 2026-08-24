import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from hardware import motor_helpers
from hardware.motor import Motor, MotorStatus
from hardware.exceptions import MotorConfigError, MotorError
from hardware.servo import Servo

def make_motor_params(**overrides):
    params = {
        'motor_id': 1,
        'lim': 100.0,
        'home_rpm': 100,
        'ret_rpm': 200,
        'rpm': 300,
        'accel': 100,
        'Ip': 10,
        'dr': 0,
        'mpr': 80.0,
        'phys_at_zero': 0.0,
        'phys_at_max': 100.0,
        'status_reg': 0x1003,
        'status_fault_bit': 0,
        'status_complete_bit': 5,
        'ppr': 10000,
        'max_following_error': 4000,
    }
    params.update(overrides)
    return SimpleNamespace(**params)


def configure_readback_registers(
    overrides: dict[int, int] | None = None,
) -> dict[int, int]:
    registers = {
        0x0003: 2,
        0x0001: 10000,
        0x0233: 4000,
        0x0191: 10,
    }
    if overrides is not None:
        registers.update(overrides)
    return registers




@pytest.mark.asyncio
async def test_servo_initialization(mock_controller):
    servo = Servo(mock_controller)
    assert servo._motor_id == 0x9
    assert servo._controller == mock_controller
    assert servo._control_reg == 0x3E8
    assert servo._status_reg == 0x07D0


@pytest.mark.asyncio
async def test_servo_configure(mock_controller):
    servo = Servo(mock_controller)
    await servo.configure()

    calls = mock_controller.write_single_register.call_args_list
    assert len(calls) >= 8
    assert calls[0] == ((0x9, 0x3E8, 0x0),)
    assert calls[1] == ((0x9, 0x3E8, 0x1),)


@pytest.mark.asyncio
async def test_servo_open(mock_controller):
    servo = Servo(mock_controller)
    await servo.open()

    assert mock_controller.write_single_register.await_args_list == [
        call(0x9, 0x3E8, 0x0),
        call(0x9, 0x3E8, 0x1),
        call(0x9, 0x03EB, 0x8020),
        call(0x9, 0x03ED, 0x80FE),
        call(0x9, 0x03EF, 0x80C0),
        call(0x9, 0x03EC, 0x80),
        call(0x9, 0x03EE, 0x80),
        call(0x9, 0x03F0, 0x80),
        call(0x9, 0x3E8, 0x080B),
    ]
    assert servo._configured is True


@pytest.mark.asyncio
async def test_servo_close(mock_controller):
    servo = Servo(mock_controller)
    await servo.close()

    assert mock_controller.write_single_register.await_args_list == [
        call(0x9, 0x3E8, 0x0),
        call(0x9, 0x3E8, 0x1),
        call(0x9, 0x03EB, 0x8020),
        call(0x9, 0x03ED, 0x80FE),
        call(0x9, 0x03EF, 0x80C0),
        call(0x9, 0x03EC, 0x80),
        call(0x9, 0x03EE, 0x80),
        call(0x9, 0x03F0, 0x80),
        call(0x9, 0x3E8, 0x090B),
    ]
    assert servo._configured is True


@pytest.mark.asyncio
async def test_motor_initialization(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    assert motor._motor_id == 1
    assert motor._controller == mock_controller
    assert motor._status == MotorStatus.IDLE


@pytest.mark.asyncio
async def test_motor_configure(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    await motor.configure()

    mock_controller.write_single_register.assert_any_call(1, 0x0191, 10)
    assert call(1, 0x0003, 2) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0001, 10000) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0233, 4000) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0159, 0xA1) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0167, 250) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0169, 250) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x016B, 10) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_motor_configure_uses_and_verifies_explicit_motor_electrical_settings(
    mock_controller,
):
    params = make_motor_params(
        motor_name="y_axis",
        brake_output_function=0x24,
        control_mode=2,
        ppr=10000,
        encoder_resolution_cpr=4000,
        Ip=60,
    )
    motor = Motor(mock_controller, params)
    expected_registers = configure_readback_registers(
        {
            0x0003: 2,
            0x0001: 10000,
            0x0233: 4000,
            0x0191: 60,
            0x0167: 250,
            0x0169: 250,
            0x016B: 10,
            0x0159: 0x24,
        }
    )
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    await motor.configure()

    mock_controller.write_single_register.assert_any_call(1, 0x0003, 2)
    mock_controller.write_single_register.assert_any_call(1, 0x0001, 10000)
    mock_controller.write_single_register.assert_any_call(1, 0x0233, 4000)
    mock_controller.write_single_register.assert_any_call(1, 0x0191, 60)


@pytest.mark.asyncio
async def test_motor_configure_fails_before_brake_assignment_on_encoder_mismatch(
    mock_controller,
):
    params = make_motor_params(
        motor_name="y_axis", brake_output_function=0x24, Ip=60
    )
    motor = Motor(mock_controller, params)
    expected_registers = configure_readback_registers({0x0233: 1000, 0x0191: 60})
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    with pytest.raises(MotorError, match="Encoder resolution readback mismatch"):
        await motor.configure()

    assert call(1, 0x0159, 0x24) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_motor_configure_writes_and_verifies_explicit_brake_output(mock_controller):
    params = make_motor_params(
        motor_name="y_axis", brake_output_function=0x24, Ip=60
    )
    motor = Motor(mock_controller, params)
    expected_registers = configure_readback_registers(
        {
            0x0191: 60,
            0x0167: 250,
            0x0169: 250,
            0x016B: 10,
            0x0159: 0x24,
        }
    )
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    await motor.configure()

    calls = mock_controller.write_single_register.await_args_list
    assert call(1, 0x0167, 250) in calls
    assert call(1, 0x0169, 250) in calls
    assert call(1, 0x016B, 10) in calls
    assert call(1, 0x0159, 0x24) in calls
    assert calls.index(call(1, 0x0167, 250)) < calls.index(call(1, 0x0159, 0x24))
    assert calls.index(call(1, 0x0169, 250)) < calls.index(call(1, 0x0159, 0x24))
    assert calls.index(call(1, 0x016B, 10)) < calls.index(call(1, 0x0159, 0x24))
    mock_controller.read_holding_registers.assert_any_call(1, 0x0159)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Ip", 59),
        ("control_mode", 0),
        ("ppr", 8000),
        ("encoder_resolution_cpr", 1000),
        ("brake_output_function", 0xA4),
        ("brake_release_delay_ms", 300),
        ("brake_engagement_delay_ms", 400),
        ("brake_engagement_speed_threshold", 12),
    ],
)
async def test_y_axis_rejects_noncanonical_runtime_profile_before_profile_writes(
    mock_controller,
    field,
    value,
):
    params_kwargs = {
        "motor_name": "y_axis",
        "brake_output_function": 0x24,
        "control_mode": 2,
        "ppr": 10000,
        "encoder_resolution_cpr": 4000,
        "Ip": 60,
    }
    params_kwargs[field] = value
    params = make_motor_params(**params_kwargs)
    motor = Motor(mock_controller, params)

    with pytest.raises(MotorConfigError, match=field):
        await motor._configure_motor_electrical_parameters()

    mock_controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_configure_rejects_brake_timing_readback_mismatch(mock_controller):
    params = make_motor_params(
        motor_name="y_axis", brake_output_function=0x24, Ip=60
    )
    motor = Motor(mock_controller, params)
    expected_registers = configure_readback_registers(
        {0x0191: 60, 0x0167: 0, 0x0169: 250, 0x016B: 10, 0x0159: 0x24}
    )
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    with pytest.raises(MotorError, match="Brake release delay readback mismatch"):
        await motor.configure()

    assert call(1, 0x0159, 0x24) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_y_axis_configure_requires_explicit_brake_output(mock_controller):
    params = make_motor_params(motor_name="y_axis")
    motor = Motor(mock_controller, params)

    with pytest.raises(MotorError, match="brake_output_function=None"):
        await motor._configure_motor_electrical_parameters()

    mock_controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_restore_brake_output_writes_even_when_cached(mock_controller):
    params = make_motor_params(brake_output_function=0x24)
    motor = Motor(mock_controller, params)
    mock_controller.read_holding_registers.return_value = 0x24

    await motor.restore_brake_output()

    mock_controller.write_single_register.assert_called_once_with(1, 0x0159, 0x24)


@pytest.mark.asyncio
async def test_brake_diagnostics_include_verified_sequencing_parameters(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24),
    )
    expected_registers = {
        0x0003: 2,
        0x0001: 10000,
        0x0233: 4000,
        0x0191: 10,
        0x0159: 0x24,
        0x0167: 250,
        0x0169: 250,
        0x016B: 10,
        0x017B: 0x02,
    }
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    assert await motor.get_brake_output_diagnostics() == {
        "control_mode": 2,
        "command_pulses_per_revolution": 10000,
        "encoder_resolution_cpr": 4000,
        "peak_current_setting": 10,
        "configured_function": 0x24,
        "register_function": 0x24,
        "release_delay_ms": 250,
        "engagement_delay_ms": 250,
        "engagement_speed_threshold": 10,
        "output_state": 0x02,
    }


def test_cs2b_reference_map_keeps_clrs_communication_bit_delay_address() -> None:
    assert motor_helpers.get_register_by_name("Comm bit delay") == 0x01C4


@pytest.mark.asyncio
async def test_motor_rejects_path_completed_as_brake_output(mock_controller):
    motor = Motor(mock_controller, make_motor_params())

    with pytest.raises(MotorError, match="Refusing non-brake SO2 function 0xA1"):
        await motor.set_brake_output_function(0xA1)

    mock_controller.write_single_register.assert_not_called()

@pytest.mark.asyncio
async def test_clear_alarm_rejects_persistent_alarm_32(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.move_config = AsyncMock()
    motor.get_param = AsyncMock(return_value=32)

    with pytest.raises(MotorError, match="Alarm 32 persists after clear attempt"):
        await motor.clear_alarm()

    assert motor.get_param.await_count == 3


@pytest.mark.asyncio
async def test_motor_status_reading(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)

    mock_controller.read_holding_registers.return_value = 0x0

    status = await motor._read_status()
    assert status == MotorStatus.IDLE
    mock_controller.read_holding_registers.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "expected_status"),
    [
        (MotorStatus.MOVING, MotorStatus.STOPPED),
        (MotorStatus.IDLE, MotorStatus.STOPPED),
        (MotorStatus.ERROR, MotorStatus.ERROR),
        (MotorStatus.FAULT, MotorStatus.FAULT),
        (MotorStatus.TIMEOUT, MotorStatus.TIMEOUT),
    ],
)
async def test_stop_preserves_terminal_status(mock_controller, initial_status, expected_status):
    motor = Motor(mock_controller, make_motor_params())
    motor._status = initial_status

    status = await motor.stop()

    mock_controller.write_single_register.assert_awaited_once_with(1, 0x6002, 0x40)
    assert status is expected_status
    assert motor._status is expected_status


@pytest.mark.asyncio
async def test_stop_preserves_fault_raised_during_stop_write(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor._is_homed = True

    async def set_fault_during_write(*_args):
        motor.set_fault()

    mock_controller.write_single_register.side_effect = set_fault_during_write

    status = await motor.stop()

    mock_controller.write_single_register.assert_awaited_once_with(1, 0x6002, 0x40)
    assert status is MotorStatus.FAULT
    assert motor._status is MotorStatus.FAULT
    assert motor.is_hard_stopped() is True
    assert motor.is_homed() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "expected_status"),
    [
        (MotorStatus.MOVING, MotorStatus.STOPPED),
        (MotorStatus.ERROR, MotorStatus.ERROR),
        (MotorStatus.FAULT, MotorStatus.FAULT),
        (MotorStatus.TIMEOUT, MotorStatus.TIMEOUT),
    ],
)
async def test_stop_and_clear_motion_preserves_terminal_status(
    mock_controller,
    initial_status,
    expected_status,
):
    motor = Motor(mock_controller, make_motor_params())
    motor._status = initial_status
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0

    await motor._stop_and_clear_motion()

    mock_controller.write_single_register.assert_awaited_once_with(1, 0x6002, 0x40)
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._status is expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [MotorStatus.MOVING, MotorStatus.FAULT])
async def test_stop_and_clear_motion_clears_motion_when_stop_write_fails(
    mock_controller,
    initial_status,
):
    motor = Motor(mock_controller, make_motor_params())
    motor._status = initial_status
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    mock_controller.write_single_register.side_effect = RuntimeError("Modbus unavailable")

    with pytest.raises(MotorError, match="Failed to stop motor 1: Modbus unavailable"):
        await motor._stop_and_clear_motion()

    mock_controller.write_single_register.assert_awaited_once_with(1, 0x6002, 0x40)
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert motor._status is initial_status


@pytest.mark.asyncio
async def test_wait_for_move_stops_and_cancels_when_abort_requested(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)

    with pytest.raises(asyncio.CancelledError):
        await motor.wait_for_move(should_abort=lambda: True)

    motor.stop.assert_awaited_once()
    mock_controller.read_holding_registers.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_move_cancelled_during_homing_marks_skip_and_stops(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    did_start_read = asyncio.Event()

    async def didReadMotionSnapshot():
        did_start_read.set()
        await asyncio.sleep(60)

    motor._read_motion_snapshot = AsyncMock(side_effect=didReadMotionSnapshot)

    task = asyncio.create_task(motor.wait_for_move(homing=True))
    await did_start_read.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert motor._should_skip_pre_home_once is True
    assert motor.is_hard_stopped() is True
    assert motor._pending_motion is None
    assert motor._target_pos is None
    motor.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_move_does_not_retry_pending_motion_after_alarm_32(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    pending_motion = AsyncMock()
    motor._pending_motion = pending_motion
    motor._target_pos = 50.0

    positions = iter([0.0, 1.0])

    async def didGetPosition():
        motor._cur_pos = next(positions)
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=didGetPosition)
    motor.get_param = AsyncMock(return_value=32)
    mock_controller.read_holding_registers.side_effect = [0, 0]

    status = await motor.wait_for_move(timeout=0.5)

    assert status == MotorStatus.FAULT
    motor.stop.assert_awaited_once()
    pending_motion.assert_not_awaited()
    assert motor._pending_motion is None


@pytest.mark.asyncio
async def test_wait_for_move_allows_alarm_32_as_homing_limit_stop(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    positions = iter([0.0, 1.0])

    async def didGetPosition():
        motor._cur_pos = next(positions)
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=didGetPosition)
    motor.get_param = AsyncMock(return_value=32)
    mock_controller.read_holding_registers.side_effect = [0, 0]

    status = await motor.wait_for_move(timeout=0.5, homing=True)

    assert status == MotorStatus.STOPPED
    motor.stop.assert_awaited_once()
    assert motor.is_hard_stopped() is False


@pytest.mark.asyncio
async def test_wait_for_move_allows_alarm_32_when_already_at_home_stop(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)

    async def didGetPosition():
        motor._cur_pos = 0.0
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=didGetPosition)
    motor.get_param = AsyncMock(return_value=32)
    mock_controller.read_holding_registers.side_effect = [0]

    status = await motor.wait_for_move(timeout=0.5, homing=True)

    assert status == MotorStatus.STOPPED
    motor.stop.assert_awaited_once()
    assert motor.is_hard_stopped() is False


@pytest.mark.asyncio
async def test_wait_for_move_rejects_non_limit_fault_during_homing(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = AsyncMock(side_effect=[0.0])
    motor.get_param = AsyncMock(return_value=5)
    mock_controller.read_holding_registers.side_effect = [0]

    status = await motor.wait_for_move(timeout=0.5, homing=True)

    assert status == MotorStatus.FAULT
    motor.stop.assert_awaited_once()
    assert motor.is_hard_stopped() is True


@pytest.mark.asyncio
async def test_motor_home_moves_near_home_before_sensorless_seek(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()

    await motor.home()

    motor.clear_alarm.assert_awaited_once()
    motor._move_near_home.assert_awaited_once_with(params.ret_rpm)


@pytest.mark.asyncio
async def test_motor_home_runs_clearance_move_before_pre_home_approach(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    events: list[str] = []
    motor.clear_alarm = AsyncMock()
    motor._move_away_from_home = AsyncMock(side_effect=lambda *_: events.append("clearance"))
    motor._move_near_home = AsyncMock(side_effect=lambda *_: events.append("approach"))
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()

    await motor.home(pre_home_clearance_mm=10.0)

    assert events == ["clearance", "approach"]
    motor._move_away_from_home.assert_awaited_once_with(10.0, params.ret_rpm)


@pytest.mark.asyncio
async def test_move_away_from_home_uses_positive_logical_direction_for_normal_mapping(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.jog = AsyncMock()

    await motor._move_away_from_home(10.0, params.ret_rpm)

    motor.jog.assert_awaited_once_with(10.0, rpm=params.ret_rpm, homing=False)


@pytest.mark.asyncio
async def test_startup_home_sequence_is_clearance_then_approach_then_seek(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    events: list[str] = []
    motor.clear_alarm = AsyncMock()
    motor.jog = AsyncMock(side_effect=lambda distance, **_: events.append(f"jog:{distance}"))
    motor.get_position = AsyncMock(return_value=10.0)
    motor.move_to_position = AsyncMock(side_effect=lambda target, **_: events.append(f"move:{target}"))
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock(side_effect=lambda: events.append("seek"))
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.move_config = AsyncMock()

    await motor.home(pre_home_clearance_mm=10.0)

    assert events == ["jog:10.0", "move:5.0", "seek"]


@pytest.mark.asyncio
async def test_diagnostic_startup_home_sequence_is_clearance_then_direct_seek(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    events: list[str] = []
    motor.clear_alarm = AsyncMock()
    motor.jog = AsyncMock(side_effect=lambda distance, **_: events.append(f"jog:{distance}"))
    motor.get_position = AsyncMock(return_value=50.0)
    motor.move_to_position = AsyncMock(side_effect=lambda target, **_: events.append(f"move:{target}"))
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock(side_effect=lambda: events.append("seek"))
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.move_config = AsyncMock()
    active_peak_current = params.Ip

    async def set_param(name: str, value: int) -> None:
        nonlocal active_peak_current
        if name == "Peak current":
            active_peak_current = value
            events.append(f"current:{value}")

    async def get_param(name: str) -> int:
        assert name == "Peak current"
        return active_peak_current

    motor.set_param = AsyncMock(side_effect=set_param)
    motor.get_param = AsyncMock(side_effect=get_param)

    await motor.home(
        pre_home_clearance_mm=10.0,
        skip_pre_home_approach=True,
        homing_peak_current=30,
    )

    assert events == ["jog:10.0", "current:30", "seek", f"current:{params.Ip}"]
    motor.get_position.assert_not_awaited()
    motor.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnostic_homing_restores_peak_current_after_start_failure(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    active_peak_current = params.Ip
    motor.clear_alarm = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock(side_effect=MotorError("start failed"))

    async def set_param(name: str, value: int) -> None:
        nonlocal active_peak_current
        assert name == "Peak current"
        active_peak_current = value

    async def get_param(name: str) -> int:
        assert name == "Peak current"
        return active_peak_current

    motor.set_param = AsyncMock(side_effect=set_param)
    motor.get_param = AsyncMock(side_effect=get_param)

    with pytest.raises(MotorError, match="Failed to start homing movement"):
        await motor.home(skip_pre_home_approach=True, homing_peak_current=30)

    assert active_peak_current == params.Ip
    assert [call.args for call in motor.set_param.await_args_list] == [
        ("Peak current", 30),
        ("Peak current", params.Ip),
    ]


@pytest.mark.asyncio
async def test_diagnostic_homing_restores_peak_current_after_cancellation(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    active_peak_current = params.Ip
    motor.clear_alarm = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor.wait_for_move = AsyncMock(side_effect=asyncio.CancelledError())

    async def set_param(name: str, value: int) -> None:
        nonlocal active_peak_current
        assert name == "Peak current"
        active_peak_current = value

    async def get_param(name: str) -> int:
        assert name == "Peak current"
        return active_peak_current

    motor.set_param = AsyncMock(side_effect=set_param)
    motor.get_param = AsyncMock(side_effect=get_param)

    with pytest.raises(asyncio.CancelledError):
        await motor.home(skip_pre_home_approach=True, homing_peak_current=30)

    assert active_peak_current == params.Ip
    assert [call.args for call in motor.set_param.await_args_list] == [
        ("Peak current", 30),
        ("Peak current", params.Ip),
    ]


@pytest.mark.asyncio
async def test_diagnostic_home_can_skip_pre_home_approach(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.move_config = AsyncMock()

    await motor.home(skip_pre_home_approach=True)

    motor._move_near_home.assert_not_awaited()
    motor._start_path_zero.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_homing_marks_next_home_to_skip_pre_home(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)

    with pytest.raises(asyncio.CancelledError):
        await motor._raise_if_move_cancelled(lambda: True, homing=True)

    assert motor._should_skip_pre_home_once is True
    assert motor.is_hard_stopped() is True
    motor.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_move_near_home_skips_once_after_interrupted_homing(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor._should_skip_pre_home_once = True
    motor.get_position = AsyncMock(return_value=50.0)
    motor.move_to_position = AsyncMock()

    await motor._move_near_home(params.ret_rpm)

    assert motor._should_skip_pre_home_once is False
    motor.get_position.assert_not_awaited()
    motor.move_to_position.assert_not_awaited()

@pytest.mark.asyncio
async def test_move_near_home_uses_upper_logical_target_when_mapping_is_inverted(mock_controller):
    params = make_motor_params(
        lim=184.5,
        phys_at_zero=184.5,
        phys_at_max=0.0,
    )
    motor = Motor(mock_controller, params)
    motor.get_position = AsyncMock(return_value=0.0)
    motor.move_to_position = AsyncMock()

    await motor._move_near_home(params.ret_rpm)

    motor.move_to_position.assert_awaited_once_with(
        184.5 - 5.0,
        rpm=params.ret_rpm,
        homing=False,
    )


@pytest.mark.asyncio
async def test_move_near_home_skips_when_inverted_mapping_is_near_home(mock_controller):
    params = make_motor_params(
        lim=184.5,
        phys_at_zero=184.5,
        phys_at_max=0.0,
    )
    motor = Motor(mock_controller, params)
    motor.get_position = AsyncMock(return_value=180.0)
    motor.move_to_position = AsyncMock()

    await motor._move_near_home(params.ret_rpm)

    motor.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_near_home_skips_stage_motor(mock_controller):
    params = make_motor_params(motor_name='stage')
    motor = Motor(mock_controller, params)
    motor.get_position = AsyncMock(return_value=7.7)
    motor.move_to_position = AsyncMock()

    await motor._move_near_home(params.ret_rpm)

    motor.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_returns_last_good_position_when_garbage_persists(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        position_reg_high=0x602C,
        position_reg_low=0x602D,
        ppr=1,
        mpr=1,
        lim=10,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)
    motor._cur_pos = 7.5
    motor._logger = Mock()

    mock_controller.read_holding_register_block.side_effect = [(0, 100), (0, 100)]

    position = await motor.get_position()

    assert position == 7.5
    motor._logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_motor_get_position_caches_zero_position(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        position_reg_high=0x602C,
        position_reg_low=0x602D,
        ppr=1,
        mpr=1,
        lim=10,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)
    motor._cur_pos = 7.5

    mock_controller.read_holding_register_block.return_value = (0, 0)

    position = await motor.get_position()

    assert position == 0.0
    assert motor._cur_pos == 0.0


@pytest.mark.asyncio
async def test_motor_get_position_maps_zero_physical_position_to_logical_position(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        position_reg_high=0x602C,
        position_reg_low=0x602D,
        ppr=1,
        mpr=1,
        lim=180,
        physical_to_logical=lambda physical: 180.0 - physical,
    )
    motor = Motor(mock_controller, params)
    motor._cur_pos = 7.5

    mock_controller.read_holding_register_block.return_value = (0, 0)

    position = await motor.get_position()

    assert position == 180.0
    assert motor._cur_pos == 180.0


@pytest.mark.asyncio
async def test_motor_absolute_move_uses_logical_to_physical_mapping(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        lim=180.0,
        lower_lim=0.0,
        mpr=1.0,
        ppr=1,
        rpm=300,
        accel=100,
        logical_to_physical=lambda logical: 180.0 - logical,
    )
    motor = Motor(mock_controller, params)

    await motor.move_to_position_no_wait(0.0)

    mock_controller.write_single_register.assert_any_call(1, 0x6201, 0)
    mock_controller.write_single_register.assert_any_call(1, 0x6202, 180)


@pytest.mark.asyncio
async def test_motor_absolute_move_skips_same_position_path_trigger(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._cur_pos = 42.0

    did_start = await motor.move_to_position_no_wait(42.005)

    assert did_start is False
    assert motor._status is MotorStatus.IDLE
    assert motor._pending_motion is None
    assert motor._target_pos is None
    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "hard_stopped", "is_homed", "has_active_motion"),
    [
        (MotorStatus.MOVING, False, True, True),
        (MotorStatus.ERROR, True, False, False),
        (MotorStatus.FAULT, True, False, False),
        (MotorStatus.TIMEOUT, True, False, False),
    ],
)
async def test_motor_absolute_move_noop_preserves_existing_motion_and_safety_state(
    mock_controller,
    initial_status,
    hard_stopped,
    is_homed,
    has_active_motion,
):
    motor = Motor(mock_controller, make_motor_params(motor_name="y_axis"))
    motor._cur_pos = 42.0
    motor._status = initial_status
    motor._hard_stopped = hard_stopped
    motor._is_homed = is_homed
    pending_motion = AsyncMock() if has_active_motion else None
    target_pos = 50.0 if has_active_motion else None
    motor._pending_motion = pending_motion
    motor._target_pos = target_pos

    await motor.move_to_position(42.005)

    assert motor._status is initial_status
    assert motor._hard_stopped is hard_stopped
    assert motor._is_homed is is_homed
    assert motor._pending_motion is pending_motion
    assert motor._target_pos == target_pos
    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_motor_absolute_move_triggers_outside_noop_tolerance(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0xA4,
            logical_to_physical=lambda value: value,
        ),
    )
    motor._cur_pos = 42.0
    mock_controller.read_holding_registers.return_value = 0xA4

    did_start = await motor.move_to_position_no_wait(42.02)

    assert did_start is True
    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)
    assert motor._active_brake_output_function == 0xA4


@pytest.mark.asyncio
async def test_y_axis_absolute_move_requires_explicit_brake_configuration(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", logical_to_physical=lambda value: value),
    )

    with pytest.raises(MotorConfigError, match="no verified brake output configuration"):
        await motor.move_to_position_no_wait(42.02)

    mock_controller.read_holding_registers.assert_not_awaited()
    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_y_axis_motion_rejects_mismatched_brake_readback_without_trigger(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            logical_to_physical=lambda value: value,
        ),
    )
    mock_controller.read_holding_registers.return_value = 0xA4

    with pytest.raises(MotorConfigError, match="SO2 readback mismatch"):
        await motor.move_to_position_no_wait(42.02)

    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list
    assert motor._active_brake_output_function is None


@pytest.mark.asyncio
async def test_y_axis_jog_triggers_after_fresh_brake_readback(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            logical_to_physical_delta=lambda value: value,
        ),
    )
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    mock_controller.read_holding_registers.return_value = 0x24

    status = await motor.jog(1.0)

    assert status is MotorStatus.STOPPED
    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)
    assert motor._active_brake_output_function == 0x24


@pytest.mark.asyncio
async def test_y_axis_home_triggers_after_fresh_brake_readback(mock_controller):
    params = make_motor_params(motor_name="y_axis", brake_output_function=0xA4)
    motor = Motor(mock_controller, params)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    mock_controller.read_holding_registers.return_value = 0xA4

    await motor.home()

    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)
    assert motor._active_brake_output_function == 0xA4


@pytest.mark.asyncio
async def test_motor_get_position_decodes_signed_32_bit_values(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        position_reg_high=0x602C,
        position_reg_low=0x602D,
        ppr=10000,
        mpr=50,
        lim=75,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)

    mock_controller.read_holding_register_block.return_value = (0xFFFF, 0xFFDC)

    position = await motor.get_position()

    assert position == pytest.approx(-0.18)
    assert motor._cur_pos == pytest.approx(-0.18)


@pytest.mark.asyncio
async def test_motor_get_position_reads_contiguous_registers_as_block(mock_controller):
    params = SimpleNamespace(
        motor_id=31,
        position_reg_high=0x602E,
        position_reg_low=0x602F,
        ppr=10000,
        mpr=12,
        lim=100,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)

    mock_controller.read_holding_register_block.return_value = (1, 0)

    position = await motor.get_position()

    assert position == pytest.approx(78.6432)
    mock_controller.read_holding_register_block.assert_awaited_once_with(31, 0x602E, count=2)
    mock_controller.read_holding_registers.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_get_position_reads_reversed_contiguous_registers_as_block(mock_controller):
    params = SimpleNamespace(
        motor_id=31,
        position_reg_high=0x602E,
        position_reg_low=0x602D,
        ppr=10000,
        mpr=12,
        lim=100,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)

    mock_controller.read_holding_register_block.return_value = (0, 1)

    position = await motor.get_position()

    assert position == pytest.approx(78.6432)
    mock_controller.read_holding_register_block.assert_awaited_once_with(31, 0x602D, count=2)
    mock_controller.read_holding_registers.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_get_position_falls_back_to_separate_reads_for_noncontiguous_registers(mock_controller):
    params = SimpleNamespace(
        motor_id=31,
        position_reg_high=0x602E,
        position_reg_low=0x6031,
        ppr=10000,
        mpr=12,
        lim=100,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)

    mock_controller.read_holding_registers.side_effect = [0, 1]

    position = await motor.get_position()

    assert position == pytest.approx(78.6432)
    mock_controller.read_holding_register_block.assert_not_awaited()


@pytest.mark.asyncio
async def test_motor_throttles_repeated_garbage_position_logs(mock_controller):
    params = SimpleNamespace(
        motor_id=1,
        position_reg_high=0x602C,
        position_reg_low=0x602D,
        ppr=1,
        mpr=1,
        lim=10,
        physical_to_logical=lambda physical: physical,
    )
    motor = Motor(mock_controller, params)
    motor._cur_pos = 7.5
    motor._logger = Mock()

    mock_controller.read_holding_register_block.side_effect = [
        (0, 100),
        (0, 100),
        (0, 100),
        (0, 100),
    ]

    first = await motor.get_position()
    second = await motor.get_position()

    assert first == 7.5
    assert second == 7.5
    motor._logger.warning.assert_called_once()
