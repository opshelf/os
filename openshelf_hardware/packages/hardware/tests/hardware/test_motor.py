import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from hardware import motor_helpers
from hardware.motor import (
    BRAKE_RELEASE_MARGIN_S,
    MotionAlarmState,
    MotionFaultState,
    MotionRecordEntry,
    MotionSnapshot,
    MotionWaitState,
    Motor,
    MotorStatus,
    _prune_motion_fault_dumps,
)
from hardware.exceptions import MotorConfigError, MotorError, MotorLimitError
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
        0x0145: 0x80,  # SI1 inert, so the persistent audit matches by default
        **{address: 0 for address in range(0x0147, 0x0152, 2)},  # SI2-SI7 unassigned
        0x000F: 0,  # Pr0.07, parked
        0x1003: 0,  # motion status: disabled, so the boot park confirms at once
        0x1901: 0x5555,  # EEPROM save status: OK
    }
    if overrides is not None:
        registers.update(overrides)
    return registers


def y_axis_params(**overrides):
    """A y-axis motor whose profile matches Y_AXIS_RUNTIME_PROFILE."""
    params = {
        "motor_name": "y_axis",
        "brake_output_function": 0x24,
        "control_mode": 2,
        "ppr": 10000,
        "encoder_resolution_cpr": 4000,
        "Ip": 60,
    }
    params.update(overrides)
    return make_motor_params(**params)


def persistent_brake_registers(overrides: dict[int, int] | None = None) -> dict[int, int]:
    """Readback map where the y-axis persistent brake config already matches."""
    return configure_readback_registers(
        {
            0x0191: 60,
            0x0167: 250,
            0x0169: 250,
            0x016B: 10,
            0x0159: 0x24,
            0x0145: 0x80,
            **(overrides or {}),
        }
    )



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
async def test_motor_configure(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    await motor.configure()

    mock_controller.write_single_register.assert_any_call(1, 0x0191, 10)
    assert call(1, 0x0003, 2) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0001, 10000) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0233, 4000) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0159, 0x24) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0167, 250) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0169, 250) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x016B, 10) not in mock_controller.write_single_register.await_args_list
    assert call(1, 0x0145, 0x80) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_motor_configure_uses_and_verifies_explicit_motor_electrical_settings(
    mock_controller,
):
    motor = Motor(mock_controller, y_axis_params())
    expected_registers = persistent_brake_registers()
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
    motor = Motor(mock_controller, y_axis_params())
    expected_registers = configure_readback_registers({0x0233: 1000, 0x0191: 60})
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    with pytest.raises(MotorError, match="Encoder resolution readback mismatch"):
        await motor.configure()

    assert call(1, 0x0159, 0x24) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_matching_persistent_brake_config_writes_nothing_and_skips_eeprom(
    mock_controller,
):
    """The EEPROM-wear guarantee: a drive that already agrees is only read."""
    motor = Motor(mock_controller, y_axis_params())
    expected_registers = persistent_brake_registers()
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    await motor.configure()

    calls = mock_controller.write_single_register.await_args_list
    for address in (0x0167, 0x0169, 0x016B, 0x0159, 0x0145):
        assert not any(c.args[1] == address for c in calls), hex(address)
    assert call(1, 0x1801, 0x2211) not in calls


@pytest.mark.asyncio
async def test_mismatched_persistent_brake_config_repairs_in_order_and_saves(
    mock_controller,
):
    motor = Motor(mock_controller, y_axis_params())
    # SO2 still carries the old inverted path-complete function.
    expected_registers = persistent_brake_registers()
    reads = {**expected_registers, 0x0159: 0xA1}

    def read(_unit, address):
        value = reads[address]
        # After the repair writes SO2, the drive reports the new value.
        if address == 0x0159:
            reads[address] = 0x24
        return value

    mock_controller.read_holding_registers.side_effect = read

    await motor.configure()

    calls = mock_controller.write_single_register.await_args_list
    order = {
        "enable_on": calls.index(call(1, 0x000F, 1)),
        "release": calls.index(call(1, 0x0167, 250)),
        "engage": calls.index(call(1, 0x0169, 250)),
        "speed": calls.index(call(1, 0x016B, 10)),
        "so2": calls.index(call(1, 0x0159, 0x24)),
        "si1": calls.index(call(1, 0x0145, 0x80)),
        "save": calls.index(call(1, 0x1801, 0x2211)),
    }
    # Energise first, sequencing before SO2, DI1 inert, park, then commit.
    assert order["enable_on"] < order["release"] < order["so2"]
    assert order["engage"] < order["so2"]
    assert order["speed"] < order["so2"]
    # Pr0.07 is back at 0 before the save, so the EEPROM never stores a forced enable.
    assert order["so2"] < order["si1"] < calls.index(call(1, 0x000F, 0)) < order["save"]
    mock_controller.read_holding_registers.assert_any_call(1, 0x1901)


@pytest.mark.asyncio
async def test_repairing_di1_demands_a_drive_power_cycle(mock_controller, caplog):
    """Manual 4.3.2 note 2: an input function only takes effect after a restart."""
    motor = Motor(mock_controller, y_axis_params())
    reads = persistent_brake_registers({0x0145: 0x88})

    def read(_unit, address):
        value = reads[address]
        if address == 0x0145:
            reads[address] = 0x80
        return value

    mock_controller.read_holding_registers.side_effect = read

    await motor.configure()

    assert "POWER-CYCLE THE DRIVE" in caplog.text


@pytest.mark.asyncio
async def test_enable_on_another_input_is_cleared_keeping_polarity(mock_controller, caplog):
    """An unwired N.C. ENABLE on any input holds the drive enabled when Pr0.07=0."""
    motor = Motor(mock_controller, y_axis_params())
    registers = echo_registers(mock_controller, persistent_brake_registers({0x0149: 0x0388}))

    await motor.configure()

    calls = mock_controller.write_single_register.await_args_list
    assert registers[0x0149] == 0x0380  # SI3 inert, N.C. and 20 ms filter kept
    assert calls.index(call(1, 0x0149, 0x0380)) < calls.index(call(1, 0x1801, 0x2211))
    assert not any(c.args[1] in (0x0147, 0x014B, 0x014D, 0x014F, 0x0151) for c in calls)
    assert "POWER-CYCLE THE DRIVE" in caplog.text


@pytest.mark.asyncio
async def test_non_parked_axis_leaves_other_enable_inputs_alone(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="z_axis", brake_output_function=0x24))
    echo_registers(mock_controller, persistent_brake_registers({0x0191: 10, 0x0149: 0x88}))

    await motor.configure()

    calls = mock_controller.write_single_register.await_args_list
    assert not any(c.args[1] in (0x0145, 0x0149, 0x000F) for c in calls)


@pytest.mark.asyncio
async def test_configure_parks_a_parked_axis(mock_controller):
    """A drive restored from an EEPROM holding Pr0.07=1 comes up on its brake."""
    motor = Motor(mock_controller, y_axis_params())
    registers = echo_registers(mock_controller, persistent_brake_registers({0x000F: 1, 0x1003: 1 << 1}))

    await motor.configure()

    assert enable_writes(mock_controller) == [0]
    assert registers[0x000F] == 0
    assert call(1, 0x1801, 0x2211) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_configure_never_parks_an_unparked_axis(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="x_axis"))
    echo_registers(mock_controller, configure_readback_registers())

    await motor.configure()

    assert enable_writes(mock_controller) == []


@pytest.mark.asyncio
async def test_persistent_brake_repair_raises_when_eeprom_save_fails(mock_controller):
    motor = Motor(mock_controller, y_axis_params())
    reads = persistent_brake_registers({0x1901: 0xAAAA})

    def read(_unit, address):
        value = reads[address]
        if address == 0x0159:
            reads[address] = 0x24
        return value

    reads[0x0159] = 0xA1
    mock_controller.read_holding_registers.side_effect = read

    with pytest.raises(MotorError, match="EEPROM save failure"):
        await motor.configure()


@pytest.mark.asyncio
async def test_persistent_brake_repair_raises_when_the_drive_rejects_a_write(
    mock_controller,
):
    motor = Motor(mock_controller, y_axis_params())
    # SO2 never takes the new value, so the post-write audit still disagrees.
    expected_registers = persistent_brake_registers({0x0159: 0xA1})
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    with pytest.raises(MotorError, match="rejected the brake configuration"):
        await motor.configure()

    assert call(1, 0x1801, 0x2211) not in mock_controller.write_single_register.await_args_list


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
    motor = Motor(mock_controller, y_axis_params(**{field: value}))

    with pytest.raises(MotorConfigError, match=field):
        await motor._configure_motor_electrical_parameters()

    mock_controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_y_axis_configure_requires_explicit_brake_output(mock_controller):
    params = make_motor_params(motor_name="y_axis")
    motor = Motor(mock_controller, params)

    with pytest.raises(MotorError, match="brake_output_function=None"):
        await motor._configure_motor_electrical_parameters()

    mock_controller.write_single_register.assert_not_awaited()


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
        0x000F: 0,
        0x0145: 0x80,
        0x0179: 0x01,
        0x1003: 0x20,  # complete, enable bit clear
    }
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: expected_registers[address]
    )

    assert await motor.get_brake_output_diagnostics() == {
        "enable_input_function": 0x80,
        "software_enable": 0,
        "digital_input_state": 0x01,
        "drive_enabled": 0,
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
@pytest.mark.parametrize("function", [0xA0, 0xA1, 0x21])
async def test_motor_rejects_unsupported_brake_output_function(mock_controller, function):
    """0xA1 is inverted path-complete -- a status signal, not a brake output."""
    motor = Motor(mock_controller, make_motor_params(brake_output_function=function))

    with pytest.raises(MotorError, match="no verified brake output configuration"):
        motor.require_brake_output_configuration()

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
async def test_move_to_position_propagates_fault_context_from_wait_fault(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(logical_to_physical=lambda value: value),
    )
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._dump_flight_recorder = Mock(return_value="fault.json")

    positions = iter([0.0, 1.0])

    async def get_position() -> float:
        motor._cur_pos = next(positions)
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=get_position)
    motor.get_param = AsyncMock(return_value=32)
    mock_controller.read_holding_registers.return_value = 0

    with pytest.raises(MotorError, match="status=FAULT") as exc_info:
        await motor.move_to_position(50.0)

    assert exc_info.value.fault_context == {
        "motor_id": 1,
        "status_word": 0,
        "alarm_code": 32,
        "stall_detected": False,
        "no_progress": False,
        "safety_blocked": False,
        "flight_recorder_path": "fault.json",
    }


@pytest.mark.asyncio
async def test_wait_for_move_generic_exception_marks_fault_mode_unknown_and_safety_blocked(
    mock_controller,
):
    safety = SimpleNamespace(blocked=True, wait_clear=AsyncMock())
    motor = Motor(mock_controller, make_motor_params(), safety=safety)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._start_absolute_move = AsyncMock(return_value=True)
    motor._read_motion_snapshot = AsyncMock(side_effect=RuntimeError("read failed"))
    motor.get_param = AsyncMock(return_value=32)
    motor._dump_flight_recorder = Mock(return_value="exception.json")

    with pytest.raises(MotorError, match="read failed") as exc_info:
        await motor.move_to_position(50.0)

    assert exc_info.value.fault_context == {
        "motor_id": 1,
        "status_word": None,
        "alarm_code": 32,
        "stall_detected": None,
        "no_progress": None,
        "safety_blocked": True,
        "stop_error": None,
        "flight_recorder_path": "exception.json",
        "position_known": False,
    }


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
@pytest.mark.parametrize(
    "home_limit_input",
    [
        pytest.param(None, id="no-input"),
        pytest.param(SimpleNamespace(name="x_lim_min", read=lambda: True), id="engaged"),
    ],
)
async def test_home_limit_verify_accepts_an_engaged_or_absent_input(
    mock_controller, home_limit_input
):
    motor = Motor(mock_controller, make_motor_params(), home_limit_input=home_limit_input)
    motor._is_homed = True
    motor._hard_stopped = False

    await motor._verify_home_limit_engaged()

    assert motor.is_homed() is True
    assert motor.is_hard_stopped() is False


@pytest.mark.asyncio
async def test_home_fails_when_the_limit_input_never_engages(mock_controller, monkeypatch):
    # The module constant, not real time, drives the poll timeout.
    monkeypatch.setattr("hardware.motor.HOME_LIMIT_VERIFY_TIMEOUT_S", 0.0)
    limit_input = SimpleNamespace(name="x_lim_min", read=lambda: False)
    motor = Motor(mock_controller, make_motor_params(), home_limit_input=limit_input)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.move_config = AsyncMock()
    motor._park = AsyncMock()

    with pytest.raises(MotorLimitError) as excinfo:
        await motor.home()

    assert "x_lim_min" in str(excinfo.value)
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True
    # The interlock must fail before the axis reports itself homed and parked.
    motor._park.assert_not_awaited()


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
    motor.is_drive_enabled = AsyncMock(return_value=True)
    mock_controller.read_holding_registers.return_value = 0xA4

    did_start = await motor.move_to_position_no_wait(42.02)

    assert did_start is True
    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)


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
    # The brake guard runs before the enable, so nothing was energised.
    assert call(1, 0x000F, 1) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_y_axis_jog_triggers_after_fresh_brake_readback(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            logical_to_physical_delta=lambda value: value,
        ),
    )
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    motor.is_drive_enabled = AsyncMock(return_value=True)
    mock_controller.read_holding_registers.return_value = 0x24

    status = await motor.jog(1.0)

    assert status is MotorStatus.STOPPED
    mock_controller.read_holding_registers.assert_awaited_once_with(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)


@pytest.mark.asyncio
async def test_y_axis_home_triggers_after_fresh_brake_readback(mock_controller):
    params = make_motor_params(
        motor_name="y_axis",
        brake_output_function=0x24,
        brake_release_delay_ms=0,
        physical_to_logical=lambda value: value,
    )
    motor = Motor(mock_controller, params)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    echo_registers(mock_controller, {0x0159: 0x24})

    await motor.home()

    mock_controller.read_holding_registers.assert_any_call(1, 0x0159)
    mock_controller.write_single_register.assert_any_call(1, 0x6002, 0x10)
    # Homing parks after the zero write, not inside wait_for_move.
    calls = mock_controller.write_single_register.await_args_list
    assert calls.index(call(1, 0x6002, 0x21)) < calls.index(call(1, 0x000F, 0))


def echo_registers(mock_controller, registers: dict[int, int]) -> dict[int, int]:
    """Back register reads with a dict that writes update.

    0x6002 is a command register whose read is drive status, so writes to it are not echoed.
    Pr0.07 is mirrored into the 0x1003 enable bit.
    """
    async def write(_unit, address, value):
        if address == 0x000F:
            registers[0x1003] = (registers.get(0x1003, 0) & ~(1 << 1)) | (value << 1)
        if address != motor_helpers.TRIGGER:
            registers[address] = value

    mock_controller.write_single_register.side_effect = write
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: registers.get(address, 0)
    )
    mock_controller.read_holding_register_block.side_effect = (
        lambda _unit, address, count=1: tuple(registers.get(address + i, 0) for i in range(count))
    )
    return registers


def y_home_motor(mock_controller, **overrides) -> Motor:
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            physical_to_logical=lambda value: value,
            **overrides,
        ),
    )
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()
    return motor


@pytest.mark.asyncio
async def test_y_axis_home_arms_following_error_trip_before_motion(mock_controller):
    motor = y_home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x0159: 0x24, 0x016D: 0x03})

    await motor.home()

    calls = mock_controller.write_single_register.await_args_list
    trigger = calls.index(call(1, 0x6002, 0x10))
    # Bit2 added, existing bits kept; 10 mm / 80 mm-per-rev * 4000 counts = 500.
    assert calls.index(call(1, 0x016D, 0x07)) < trigger
    assert calls.index(call(1, 0x000B, 500)) < trigger
    assert registers[0x016D] == 0x07
    # Homing-only: restored to max_following_error once the zero is set.
    assert calls.index(call(1, 0x6002, 0x21)) < calls.index(call(1, 0x000B, 4000))
    assert registers[0x000B] == 4000


@pytest.mark.asyncio
async def test_y_axis_home_refuses_to_move_when_trip_readback_mismatches(mock_controller):
    motor = y_home_motor(mock_controller)
    registers = {0x0159: 0x24}

    async def write(_unit, address, value):
        # Drive ignores Pr0.05.
        if address != 0x000B:
            registers[address] = value

    mock_controller.write_single_register.side_effect = write
    mock_controller.read_holding_registers.side_effect = (
        lambda _unit, address: registers.get(address, 0)
    )

    with pytest.raises(MotorConfigError, match="Failed to arm following-error trip"):
        await motor.home()

    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_y_axis_home_zeroes_only_after_clearing_the_trip(mock_controller):
    motor = y_home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x0159: 0x24})
    events: list[str] = []

    async def move_config():
        events.append("move_config")

    echo_write = mock_controller.write_single_register.side_effect

    async def write(unit, address, value):
        await echo_write(unit, address, value)
        if (address, value) == (0x6002, 0x21):
            events.append("zero")

    motor.move_config = AsyncMock(side_effect=move_config)
    mock_controller.write_single_register.side_effect = write

    await motor.home()

    assert events == ["move_config", "zero"]
    assert motor.is_homed() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsettled",
    [
        {0x2203: 0x20},  # trip still latched
        {0x1003: 1 << 2},  # still running
        {0x6002: 0x40},  # quick stop still latched; 0x21 would be dropped
    ],
)
async def test_y_axis_home_refuses_to_zero_an_unsettled_drive(mock_controller, monkeypatch, unsettled):
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    motor = y_home_motor(mock_controller)
    echo_registers(mock_controller, {0x0159: 0x24, **unsettled})

    with pytest.raises(MotorError, match="did not settle before zeroing"):
        await motor.home()

    assert call(1, 0x6002, 0x21) not in mock_controller.write_single_register.await_args_list
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_y_axis_home_verifies_the_zero_took(mock_controller):
    motor = y_home_motor(mock_controller)
    # 2500 pulses at 80 mm/rev = 20 mm, cleared by the 0x21 write.
    registers = echo_registers(mock_controller, {0x0159: 0x24, 0x602D: 2500})
    write = mock_controller.write_single_register.side_effect

    async def zeroing_write(unit, address, value):
        await write(unit, address, value)
        if (address, value) == (0x6002, 0x21):
            registers[0x602C] = registers[0x602D] = 0

    mock_controller.write_single_register.side_effect = zeroing_write

    await motor.home()

    assert motor.is_homed() is True
    assert motor._cur_pos == 0.0


@pytest.mark.asyncio
async def test_y_axis_home_fails_when_the_drive_drops_the_zero(mock_controller, monkeypatch):
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    motor = y_home_motor(mock_controller)
    echo_registers(mock_controller, {0x0159: 0x24, 0x602D: 2500})

    with pytest.raises(MotorError, match="zero did not take: position=20.000 mm"):
        await motor.home()

    assert motor.is_homed() is False
    assert mock_controller.write_single_register.await_args_list[-1] == call(1, 0x000B, 4000)
    # Never parked on a bad zero.
    assert call(1, 0x000F, 0) not in mock_controller.write_single_register.await_args_list


@pytest.mark.asyncio
async def test_y_axis_home_fails_when_following_error_limit_cannot_be_restored(mock_controller):
    motor = y_home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x0159: 0x24})
    write = mock_controller.write_single_register.side_effect

    async def refuse_restore(unit, address, value):
        if (address, value) != (0x000B, 4000):
            await write(unit, address, value)

    mock_controller.write_single_register.side_effect = refuse_restore

    with pytest.raises(MotorConfigError, match="Failed to restore motor 1 following error limit"):
        await motor.home()

    assert registers[0x000B] == 500
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True


@pytest.mark.asyncio
async def test_y_axis_home_restores_peak_current_even_when_following_error_restore_fails(mock_controller):
    motor = y_home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x0159: 0x24})
    write = mock_controller.write_single_register.side_effect

    async def refuse_following_error_restore(unit, address, value):
        if (address, value) != (0x000B, 4000):
            await write(unit, address, value)

    mock_controller.write_single_register.side_effect = refuse_following_error_restore

    with pytest.raises(MotorConfigError, match="following error limit"):
        await motor.home(homing_peak_current=5)

    assert call(1, 0x0191, 5) in mock_controller.write_single_register.await_args_list
    assert registers[0x0191] == 10  # restored to Ip, not left at the homing override


@pytest.mark.asyncio
async def test_non_y_axis_home_leaves_following_error_trip_alone(mock_controller):
    motor = Motor(mock_controller, make_motor_params(motor_name="z_axis"))
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock()

    await motor.home()

    addresses = {c.args[1] for c in mock_controller.write_single_register.await_args_list}
    assert 0x016D not in addresses
    assert 0x000B not in addresses


def enable_writes(mock_controller) -> list[int]:
    """Every value written to Pr0.07, in order."""
    return [
        c.args[2]
        for c in mock_controller.write_single_register.await_args_list
        if c.args[1] == 0x000F
    ]


@pytest.mark.asyncio
async def test_y_axis_move_enables_first_and_parks_after(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            logical_to_physical=lambda value: value,
        ),
    )
    async def finish(**_):
        return await motor._finish_completed_move(
            False, MotionWaitState(start_time=0.0, last_movement_time=0.0)
        )

    motor.wait_for_move = AsyncMock(side_effect=finish)
    echo_registers(mock_controller, {0x0159: 0x24})
    motor.get_position = AsyncMock(return_value=42.0)
    motor.stop = AsyncMock()

    await motor.move_to_position(42.0)

    assert enable_writes(mock_controller) == [1, 0]
    calls = mock_controller.write_single_register.await_args_list
    trigger = calls.index(call(1, 0x6002, 0x10))
    assert calls.index(call(1, 0x000F, 1)) < trigger < calls.index(call(1, 0x000F, 0))


@pytest.mark.asyncio
async def test_the_enable_waits_the_configured_brake_release(mock_controller):
    """Firing PR0 into a brake that is still set is the failure this axis has a
    runbook for, so the move waits out Pr4.19 before it is commanded."""
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24,
                          brake_release_delay_ms=40),
    )
    echo_registers(mock_controller, {})

    start = asyncio.get_running_loop().time()
    await motor.enable()

    assert asyncio.get_running_loop().time() - start >= 0.04 + BRAKE_RELEASE_MARGIN_S


@pytest.mark.asyncio
async def test_enable_after_disable_waits_out_brake_engagement(mock_controller, monkeypatch):
    monkeypatch.setattr("hardware.motor.DISABLE_CONFIRM_MARGIN_S", 0.0)
    monkeypatch.setattr("hardware.motor.BRAKE_RELEASE_MARGIN_S", 0.0)
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24,
                          brake_release_delay_ms=0, brake_engagement_delay_ms=100),
    )
    echo_registers(mock_controller, {})

    start = asyncio.get_running_loop().time()
    await motor.disable()
    await motor.enable()

    assert asyncio.get_running_loop().time() - start >= 0.1


@pytest.mark.asyncio
async def test_move_is_refused_when_the_drive_never_reports_enabled(mock_controller, monkeypatch):
    monkeypatch.setattr("hardware.motor.ENABLE_CONFIRM_TIMEOUT_S", 0.05)
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24,
                          brake_release_delay_ms=0, logical_to_physical=lambda value: value),
    )
    # SO2 reads back 0x24; the 0x1003 enable bit stays clear.
    mock_controller.read_holding_registers.return_value = 0x24

    with pytest.raises(MotorError, match="did not report enabled"):
        await motor.move_to_position_no_wait(42.0)

    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list


def held_y_motor(mock_controller, monkeypatch) -> tuple[Motor, dict[int, int]]:
    monkeypatch.setattr("hardware.motor.BRAKE_RELEASE_MARGIN_S", 0.0)
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24,
                          brake_release_delay_ms=0, brake_engagement_delay_ms=0,
                          logical_to_physical=lambda value: value),
    )
    return motor, echo_registers(mock_controller, {0x0159: 0x24})


@pytest.mark.asyncio
async def test_hold_enabled_spans_moves_and_parks_once(mock_controller, monkeypatch):
    motor, _ = held_y_motor(mock_controller, monkeypatch)

    async def finish(**_):
        return await motor._finish_completed_move(
            False, MotionWaitState(start_time=0.0, last_movement_time=0.0)
        )

    motor.wait_for_move = AsyncMock(side_effect=finish)
    motor.get_position = AsyncMock(side_effect=[42.0, 43.0])
    motor.stop = AsyncMock()

    async with motor.hold_enabled():
        await motor.move_to_position(42.0)
        await motor.move_to_position(43.0)
        assert enable_writes(mock_controller) == [1]

    assert enable_writes(mock_controller) == [1, 0]
    calls = mock_controller.write_single_register.await_args_list
    triggers = [i for i, c in enumerate(calls) if c == call(1, 0x6002, 0x10)]
    assert len(triggers) == 2 and triggers[-1] < calls.index(call(1, 0x000F, 0))


@pytest.mark.asyncio
async def test_hold_reenables_a_drive_that_dropped_its_enable(mock_controller, monkeypatch):
    motor, registers = held_y_motor(mock_controller, monkeypatch)

    async with motor.hold_enabled():
        registers[0x1003] = 0
        await motor.move_to_position_no_wait(42.0)

    assert enable_writes(mock_controller) == [1, 1, 0]
    calls = mock_controller.write_single_register.await_args_list
    enables = [i for i, c in enumerate(calls) if c == call(1, 0x000F, 1)]
    assert enables[1] < calls.index(call(1, 0x6002, 0x10))


@pytest.mark.asyncio
async def test_non_parked_axis_never_touches_the_software_enable(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="x_axis", logical_to_physical=lambda v: v),
    )

    await motor.move_to_position_no_wait(42.0)
    await motor._park()

    assert enable_writes(mock_controller) == []


@pytest.mark.asyncio
async def test_noop_move_neither_enables_nor_parks(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            logical_to_physical=lambda value: value,
        ),
    )
    motor._cur_pos = 42.0

    did_start = await motor.move_to_position_no_wait(42.0)

    assert did_start is False
    assert enable_writes(mock_controller) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finisher", ["_finish_fault", "_finish_timeout"])
async def test_fault_and_timeout_leave_the_axis_energised(mock_controller, finisher):
    """The conservative state for a suspended load is still holding current."""
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24),
    )
    motor.stop = AsyncMock()
    motor._dump_flight_recorder = Mock(return_value=None)
    state = MotionWaitState(start_time=0.0, last_movement_time=0.0)

    if finisher == "_finish_fault":
        await motor._finish_fault(
            False,
            MotionSnapshot(status_word=1, is_complete=False, has_fault_bit=True),
            MotionAlarmState(alarm_code=1, is_active=True),
            MotionFaultState(is_fault=True, stall_detected=False, no_progress=False),
            state,
        )
    else:
        await motor._finish_timeout(1.0, False, state)

    assert enable_writes(mock_controller) == []


@pytest.mark.asyncio
async def test_disable_warns_when_the_drive_stays_enabled(mock_controller, caplog, monkeypatch):
    """Pr0.07=0 falls back to the IO enable, so the result is read, not assumed."""
    monkeypatch.setattr("hardware.motor.DISABLE_CONFIRM_MARGIN_S", 0.0)
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis", brake_output_function=0x24, brake_engagement_delay_ms=0
        ),
    )
    # Motion status with the enable bit still set.
    mock_controller.read_holding_registers.return_value = 1 << 1

    await motor.disable()

    assert "still reports enabled" in caplog.text


@pytest.mark.asyncio
async def test_disable_is_quiet_while_the_drive_holds_through_brake_engagement(mock_controller, caplog):
    """The drive keeps the enable bit for Pr4.20 while the brake sets."""
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis", brake_output_function=0x24, brake_engagement_delay_ms=100
        ),
    )
    mock_controller.read_holding_registers.side_effect = [1 << 1, 1 << 1, 1 << 1, 0]

    await motor.disable()

    assert "still reports enabled" not in caplog.text
    assert mock_controller.read_holding_registers.await_count == 4


@pytest.mark.asyncio
async def test_disable_is_quiet_when_the_drive_de_energises(mock_controller, caplog):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24),
    )
    mock_controller.read_holding_registers.return_value = 0

    await motor.disable()

    assert "still reports enabled" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_park_warns_and_leaves_the_move_successful(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(motor_name="y_axis", brake_output_function=0x24),
    )
    motor.disable = AsyncMock(side_effect=RuntimeError("bus gone"))

    await motor._park()

    motor.disable.assert_awaited_once()


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


def test_prune_motion_fault_dumps_removes_only_stale_files(tmp_path):
    now = 1_000_000.0
    stale = tmp_path / "20260101T000000.000000Z-no_run-motor_1-fault.json"
    stale.write_text("{}")
    os.utime(stale, (now - 40 * 86400.0, now - 40 * 86400.0))
    fresh = tmp_path / "20260922T000000.000000Z-no_run-motor_1-fault.json"
    fresh.write_text("{}")
    os.utime(fresh, (now - 1 * 86400.0, now - 1 * 86400.0))

    deleted = _prune_motion_fault_dumps(tmp_path, now=now)

    assert deleted == 1
    assert not stale.exists()
    assert fresh.exists()


def test_prune_motion_fault_dumps_enforces_max_files_by_oldest_name(tmp_path):
    now = 1_000_000.0
    names = [f"20260922T0000{i:02d}.000000Z-no_run-motor_1-fault.json" for i in range(5)]
    for name in names:
        path = tmp_path / name
        path.write_text("{}")
        os.utime(path, (now, now))

    deleted = _prune_motion_fault_dumps(tmp_path, max_files=2, now=now)

    assert deleted == 3
    assert {path.name for path in tmp_path.iterdir()} == {names[3], names[4]}


def test_prune_motion_fault_dumps_ignores_non_json_files_and_missing_dir(tmp_path):
    (tmp_path / "notes.txt").write_text("{}")
    (tmp_path / "subdir").mkdir()

    assert _prune_motion_fault_dumps(tmp_path) == 0
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "subdir").exists()
    assert _prune_motion_fault_dumps(tmp_path / "does_not_exist") == 0


@pytest.mark.asyncio
async def test_dump_flight_recorder_prunes_stale_dumps(mock_controller, tmp_path, monkeypatch):
    import hardware.motor as motor_module

    now = time.time()
    stale = tmp_path / "20260101T000000.000000Z-no_run-motor_1-fault.json"
    stale.write_text("{}")
    os.utime(stale, (now - 40 * 86400.0, now - 40 * 86400.0))
    monkeypatch.setattr(motor_module, "MOTION_FAULT_DUMP_DIR", tmp_path)

    motor = Motor(
        mock_controller,
        make_motor_params(logical_to_physical=lambda value: value),
    )
    motor._logger = Mock()
    motor._flight_recorder.append(
        MotionRecordEntry(
            timestamp="2026-09-22T00:00:00Z",
            elapsed_s=1.0,
            motor_id=1,
            motor_name=None,
            status_word=None,
            is_complete=False,
            has_fault_bit=False,
            alarm_code=None,
            alarm_active=False,
            raw_position=None,
            logical_position_mm=None,
            position_read_latency_s=None,
            position_delta_mm=None,
            seconds_since_progress=0.1,
            command_target_mm=None,
            command_start_mm=None,
            command_rpm=None,
            command_accel=None,
            safety_blocked=False,
            run_id=None,
            correlation_id=None,
        )
    )
    state = MotionWaitState(start_time=now, last_movement_time=now)

    path = motor._dump_flight_recorder(reason="fault", state=state)

    assert path is not None
    assert Path(path).exists()
    assert not stale.exists()


@pytest.mark.asyncio
async def test_jog_reports_the_axis_in_motion_while_it_runs(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            logical_to_physical_delta=lambda value: value,
        ),
    )
    seen = []

    async def fake_wait_for_move(*args, **kwargs):
        # The weight-change guard reads this, so the pre-home clearance jog has to
        # look like motion while the drive is running the relative move.
        seen.append(motor.is_moving())
        await motor.stop()

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor.move_config = AsyncMock()
    motor.is_drive_enabled = AsyncMock(return_value=True)
    mock_controller.read_holding_registers.return_value = 0x24

    status = await motor.jog(1.0)

    assert status is MotorStatus.STOPPED
    assert seen == [True]
    assert motor.is_moving() is False


@pytest.mark.asyncio
async def test_jog_fails_when_the_safety_guard_stops_it(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            logical_to_physical_delta=lambda value: value,
        ),
        safety=SimpleNamespace(blocked=True, wait_clear=AsyncMock()),
    )

    async def fake_wait_for_move(*args, **kwargs):
        # The guard is blocked, so the motion monitor stops the drive and then has
        # no relative move it is allowed to restart.
        await motor._pause_for_safety_if_needed(SimpleNamespace(reset=lambda _t: None))

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor.move_config = AsyncMock()
    motor.is_drive_enabled = AsyncMock(return_value=True)
    mock_controller.read_holding_registers.return_value = 0x24

    with pytest.raises(MotorError, match="safety guard"):
        await motor.jog(1.0)

