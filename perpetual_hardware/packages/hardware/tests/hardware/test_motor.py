import pytest
import asyncio
import numpy as np
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from hardware.motor import Motor, MotorStatus, Servo

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
    
    # Verify configuration sequence
    calls = mock_controller.write_single_register.call_args_list
    assert len(calls) >= 8  # At least 8 configuration writes
    
    # Check first two calls (activation sequence)
    assert calls[0] == ((0x9, 0x3E8, 0x0),)  # clear
    assert calls[1] == ((0x9, 0x3E8, 0x1),)  # enable

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
    params = Mock(motor_id=1)
    motor = Motor(mock_controller, params)
    assert motor._motor_id == 1
    assert motor._controller == mock_controller
    assert motor._status == MotorStatus.IDLE

@pytest.mark.asyncio
async def test_motor_configure(mock_controller):
    params = Mock(motor_id=1)
    motor = Motor(mock_controller, params)
    await motor.configure()
    
    # Verify the basic configuration sequence
    mock_controller.write_single_register.assert_any_call(1, 0x0167, 100)
    mock_controller.write_single_register.assert_any_call(1, 0x0169, 100)
    mock_controller.write_single_register.assert_any_call(1, 0x016B, 10)

@pytest.mark.asyncio
async def test_motor_status_reading(mock_controller):
    params = Mock(motor_id=1)
    motor = Motor(mock_controller, params)
    
    # Setup mock response for status reading
    mock_controller.read_holding_registers.return_value = 0x0  # IDLE status
    
    status = await motor._read_status()
    assert status == MotorStatus.IDLE
    mock_controller.read_holding_registers.assert_called_once()


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
