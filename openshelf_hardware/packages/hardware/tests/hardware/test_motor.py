import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.motor import Motor, MotorStatus
from hardware.exceptions import MotorError
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
    mock_controller.write_single_register.assert_called_once_with(0x9, 0x3E8, 0x080B)


@pytest.mark.asyncio
async def test_servo_close(mock_controller):
    servo = Servo(mock_controller)
    await servo.close()
    mock_controller.write_single_register.assert_called_once_with(0x9, 0x3E8, 0x090B)


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

    mock_controller.write_single_register.assert_any_call(1, 0x0159, 0xA1)
    mock_controller.write_single_register.assert_any_call(1, 0x0167, 100)
    mock_controller.write_single_register.assert_any_call(1, 0x0169, 100)
    mock_controller.write_single_register.assert_any_call(1, 0x016B, 10)


@pytest.mark.asyncio
async def test_motor_disable_and_restore_brake_output(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)

    await motor.disable_brake_output()
    await motor.restore_brake_output()

    mock_controller.write_single_register.assert_any_call(1, 0x0159, 0x24)
    mock_controller.write_single_register.assert_any_call(1, 0x0159, 0xA1)


@pytest.mark.asyncio
async def test_motor_restore_brake_output_writes_even_when_cached(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)

    await motor.restore_brake_output()

    mock_controller.write_single_register.assert_called_once_with(1, 0x0159, 0xA1)

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
async def test_wait_for_move_stops_and_cancels_when_abort_requested(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)

    with pytest.raises(asyncio.CancelledError):
        await motor.wait_for_move(should_abort=lambda: True)

    motor.stop.assert_awaited_once()
    mock_controller.read_holding_registers.assert_not_called()


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
