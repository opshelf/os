from types import SimpleNamespace
from unittest.mock import AsyncMock

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
async def test_motor_home_with_sensor_sets_zero_and_marks_homed(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, True, True, False, False, True])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.is_homed() is True
    assert motor.is_hard_stopped() is False
    assert motor.jog.await_count == 1
    assert motor.move_config.await_count == 2
    assert any(call.args == (1, 0x6002, 0x21) for call in mock_controller.write_single_register.await_args_list)


@pytest.mark.asyncio
async def test_motor_home_with_sensor_releases_switch_before_first_seek(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([True, False, False, True, True, False, False, True])
    params = make_motor_params(home_sensor=make_home_sensor_config())
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.jog.await_count == 1
    assert motor.move_config.await_count == 3


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
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.move_to_position = AsyncMock(return_value=None)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    motor.move_to_position.assert_awaited_once_with(10.0)
    assert motor.move_config.await_count == 2


@pytest.mark.asyncio
async def test_motor_home_with_absolute_release_mode_rezeros_before_release(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([True, True, False, False, True, True, False, False, True])
    params = make_motor_params(
        home_sensor=make_home_sensor_config(release_mode='absolute', release_target_mm=10.0)
    )
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.move_to_position = AsyncMock(return_value=None)
    motor.get_param = AsyncMock(return_value=0)

    await motor.home()

    assert motor.move_to_position.await_args_list[0].args == (10.0,)
    assert motor.move_to_position.await_args_list[1].args == (10.0,)
    assert motor.move_config.await_count == 3


@pytest.mark.asyncio
async def test_motor_home_with_sensor_can_disable_brake_during_home(mock_controller, monkeypatch):
    sensor = DummyHomeSensor([False, False, True, True, False, False, True])
    params = make_motor_params(
        home_sensor=make_home_sensor_config(disable_brake_during_home=True)
    )
    motor = Motor(mock_controller, params)

    monkeypatch.setattr('hardware.motor.HomeSensor', lambda **_kwargs: sensor)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.jog = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_param = AsyncMock(return_value=0)
    motor.disable_brake_output = AsyncMock(return_value=None)
    motor.restore_brake_output = AsyncMock(return_value=None)

    await motor.home()

    assert motor.move_config.await_count == 2
    motor.disable_brake_output.assert_awaited_once()
    motor.restore_brake_output.assert_awaited_once()
