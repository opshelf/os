import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.exceptions import MotorTimeoutError
from hardware.motor import Motor, MotorStatus, Servo


class DummyHomeSensor:
    def __init__(self, states: list[bool]) -> None:
        self._states = list(states)
        self._index = 0

    def read(self) -> bool:
        if self._index >= len(self._states):
            return self._states[-1]

        state = self._states[self._index]
        self._index += 1
        return state

    def cleanup(self) -> None:
        return None


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
        'home_sensor': None,
    }
    params.update(overrides)
    return SimpleNamespace(**params)


def make_home_sensor_config(**overrides):
    config = {
        'enabled': True,
        'pin': 25,
        'inverted': True,
        'poll_interval_s': 0.0,
        'disable_brake_during_home': False,
        'release_mode': 'jog',
        'release_backoff_mm': 5.0,
        'release_target_mm': None,
        'precision_rpm': 50,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


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
    motor._move_near_home = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()

    await motor.home()

    motor._move_near_home.assert_awaited_once_with(params.ret_rpm)


@pytest.mark.asyncio
async def test_motor_home_with_sensor_moves_near_home_before_sensor_seek(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, True, False, True])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    motor._move_near_home.assert_awaited_once_with(params.ret_rpm)


@pytest.mark.asyncio
async def test_move_near_home_uses_release_target_when_absolute_release_is_configured(mock_controller):
    params = make_motor_params(
        home_sensor=make_home_sensor_config(release_mode='absolute', release_target_mm=12.0)
    )
    motor = Motor(mock_controller, params)
    motor.get_position = AsyncMock(return_value=40.0)
    motor.jog = AsyncMock()

    await motor._move_near_home(params.ret_rpm)

    motor.jog.assert_awaited_once_with(-28.0, rpm=params.ret_rpm, homing=False)


@pytest.mark.asyncio
async def test_motor_home_with_sensor_sets_zero_and_marks_homed(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, True, True, False, False, True])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.is_homed() is True
    assert motor.is_hard_stopped() is False
    assert motor.jog.await_count == 1
    assert motor.move_config.await_count == 1
    assert any(call.args == (1, 0x6002, 0x21) for call in mock_controller.write_single_register.await_args_list)


@pytest.mark.asyncio
async def test_motor_home_with_sensor_releases_switch_before_first_seek(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([True, False, False, True, True, False, False, True])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.jog.await_count == 2
    assert motor.move_config.await_count == 1


@pytest.mark.asyncio
async def test_motor_home_with_sensor_times_out_when_sensor_never_triggers(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, False, False])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    class FakeMonotonic:
        def __init__(self) -> None:
            self._value = 0.0

        def __call__(self) -> float:
            self._value += 200.0
            return self._value

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    monkeypatch.setattr('hardware.motor.time.monotonic', FakeMonotonic())
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    with pytest.raises(MotorTimeoutError, match='timed out waiting for sensor'):
        await motor.home()


@pytest.mark.asyncio
async def test_motor_home_with_sensor_supports_absolute_release_mode(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, True, True, False, False, True])
    params = make_motor_params(
        home_sensor=make_home_sensor_config(release_mode='absolute', release_target_mm=10.0)
    )
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.jog.await_count == 1
    assert motor.move_config.await_count == 1


@pytest.mark.asyncio
async def test_motor_home_with_absolute_release_mode_rezeros_before_release(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([True, True, False, False, True, True, False, False, True])
    params = make_motor_params(
        home_sensor=make_home_sensor_config(release_mode='absolute', release_target_mm=10.0)
    )
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.jog.await_count == 3
    assert motor.move_config.await_count == 1


@pytest.mark.asyncio
async def test_motor_home_with_sensor_can_disable_brake_during_home(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, True, True, False, False, True])
    params = make_motor_params(
        home_sensor=make_home_sensor_config(disable_brake_during_home=True)
    )
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor._move_near_home = AsyncMock()
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.disable_brake_output = AsyncMock(return_value=None)
    motor.restore_brake_output = AsyncMock(return_value=None)

    await motor.home()

    assert motor.move_config.await_count == 1
    motor.disable_brake_output.assert_awaited_once()
    motor.restore_brake_output.assert_awaited_once()

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

    mock_controller.read_holding_registers.side_effect = [100, 0, 100, 0]

    position = await motor.get_position()

    assert position == 7.5
    motor._logger.warning.assert_called_once()


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

    mock_controller.read_holding_registers.side_effect = [100, 0, 100, 0, 100, 0, 100, 0]

    first = await motor.get_position()
    second = await motor.get_position()

    assert first == 7.5
    assert second == 7.5
    motor._logger.warning.assert_called_once()
