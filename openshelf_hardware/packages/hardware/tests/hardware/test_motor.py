import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from hardware import motor_helpers
from hardware.motor import (
    BRAKE_RELEASE_MARGIN_S,
    MotionAlarmState,
    MotionFaultState,
    MotionSnapshot,
    MotionWaitState,
    Motor,
    MotorStatus,
    _prune_motion_fault_dumps,
)
from hardware.exceptions import MotorConfigError, MotorError, MotorLimitError, MotorTimeoutError
from hardware.servo import GripObject, Servo
from openshelf_os.config.models import Motor as MotorConfig

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


def _configured_servo(controller, *blocks):
    controller.read_holding_register_block.side_effect = list(blocks)
    servo = Servo(controller)
    servo._configured = True
    return servo


@pytest.mark.asyncio
async def test_servo_open_reports_contact(mock_controller):
    state = await _configured_servo(mock_controller, (0x31, 0, 0), (0x71, 0xAE00, 0xFE00)).open()
    assert state.obj is GripObject.INNER and state.gripped
    assert (state.position, state.current) == (0xAE, 0xFE)


@pytest.mark.asyncio
async def test_servo_open_without_contact_is_not_gripped(mock_controller):
    state = await _configured_servo(mock_controller, (0x31, 0, 0), (0xF1, 0x2000, 0)).open()
    assert state.obj is GripObject.NONE and not state.gripped


@pytest.mark.asyncio
async def test_servo_ignores_stale_result_until_motion_seen(mock_controller):
    # Previous move's NONE persists, then MOVING, then contact.
    servo = _configured_servo(mock_controller, (0xF1, 0, 0), (0x39, 0, 0), (0x71, 0x9000, 0x4000))
    state = await servo.open()
    assert state.obj is GripObject.INNER and state.position == 0x90


@pytest.mark.asyncio
async def test_servo_drop_flag_is_not_gripped(mock_controller):
    state = await _configured_servo(mock_controller, (0x31, 0, 0), (0x73, 0x9000, 0)).open()
    assert state.dropped and not state.gripped


@pytest.mark.asyncio
async def test_servo_fault_raises(mock_controller):
    with pytest.raises(MotorError, match="fault=0x08"):
        await _configured_servo(mock_controller, (0x31, 0x0008, 0)).open()


@pytest.mark.asyncio
async def test_servo_ignores_comm_loss_fault(mock_controller):
    state = await _configured_servo(mock_controller, (0x31, 0x0004, 0), (0x71, 0x9004, 0)).open()
    assert state.gripped


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
async def test_wait_for_move_cancelled_during_homing_marks_unhomed_and_stops(mock_controller):
    params = make_motor_params()
    motor = Motor(mock_controller, params)
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._pending_motion = AsyncMock()
    motor._target_pos = 50.0
    did_start_read = asyncio.Event()

    async def didReadMotionSnapshot(*_args):
        did_start_read.set()
        await asyncio.sleep(60)

    motor._read_motion_snapshot = AsyncMock(side_effect=didReadMotionSnapshot)

    task = asyncio.create_task(motor.wait_for_move(homing=True))
    await did_start_read.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert motor.is_homed() is False
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
async def test_wait_for_move_homing_completes_on_homed_bit_after_start(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = AsyncMock(return_value=0.0)
    motor.get_param = AsyncMock(return_value=0)
    # Running at the stop, then homed with running clear.
    mock_controller.read_holding_registers.side_effect = [1 << 2, 1 << 6]

    status = await motor.wait_for_move(timeout=0.5, homing=True)

    assert status == MotorStatus.STOPPED
    assert motor.is_hard_stopped() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "positions",
    [
        pytest.param([0.0], id="constant"),
        pytest.param([0.0, 0.008], id="one-pulse-jitter"),
    ],
)
async def test_wait_for_move_homing_ignores_a_stale_homed_bit(mock_controller, positions):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    readings = iter(positions)

    async def get_position() -> float:
        motor._cur_pos = next(readings, positions[-1])
        return motor._cur_pos

    motor.get_position = AsyncMock(side_effect=get_position)
    motor.get_param = AsyncMock(return_value=0)
    # Bit6 left over from an earlier home; the drive never starts, and encoder
    # jitter of a single pulse must not count as a homing run.
    mock_controller.read_holding_registers.return_value = 1 << 6

    status = await motor.wait_for_move(timeout=0.1, homing=True)

    assert status == MotorStatus.TIMEOUT
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_wait_for_move_homing_faults_on_alarm_32(mock_controller):
    motor = Motor(mock_controller, make_motor_params())
    motor.stop = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.get_position = AsyncMock(return_value=0.0)
    motor.get_param = AsyncMock(return_value=32)
    mock_controller.read_holding_registers.side_effect = [1 << 2]

    status = await motor.wait_for_move(timeout=0.5, homing=True)

    assert status == MotorStatus.FAULT
    assert motor.is_hard_stopped() is True


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
async def test_home_fails_when_the_limit_input_never_engages(mock_controller, monkeypatch):
    # The module constant, not real time, drives the poll timeout.
    monkeypatch.setattr("hardware.motor.HOME_LIMIT_VERIFY_TIMEOUT_S", 0.0)
    limit_input = SimpleNamespace(name="x_lim_min", read=lambda: False)
    motor = Motor(mock_controller, make_motor_params(), home_limit_input=limit_input)
    motor.clear_alarm = AsyncMock()
    motor.move_config = AsyncMock()
    motor.get_param = AsyncMock(return_value=0)
    motor._configure_drive_homing = AsyncMock()
    motor._trigger_drive_homing = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._verify_zero_position = AsyncMock()
    motor._write_verified = AsyncMock()
    motor._park = AsyncMock()

    with pytest.raises(MotorLimitError) as excinfo:
        await motor.home()

    assert "x_lim_min" in str(excinfo.value)
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True
    # The interlock must fail before the axis reports itself homed and parked.
    motor._park.assert_not_awaited()


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


def home_motor(mock_controller, **overrides) -> Motor:
    """A motor whose home() runs against echoed registers; the drive wait is mocked."""
    params = {"motor_name": "z_axis", "physical_to_logical": lambda value: value, **overrides}
    motor = Motor(mock_controller, make_motor_params(**params))
    motor.clear_alarm = AsyncMock()
    motor.move_config = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    return motor


# Pr8.x load for make_motor_params: 100 rpm, accel 100, default 200 ms / 30 %,
# overtravel 100 mm / 80 mm-per-rev * 10 * 1.25 = 15.6 -> 16 (0.1 rev).
TORQUE_HOMING_WRITES = [
    (0x600A, 0x0C),
    (0x600F, 100),
    (0x6010, 100),
    (0x6011, 100),
    (0x6012, 100),
    (0x6013, 200),
    (0x6014, 30),
    (0x6015, 16),
]


def writes(mock_controller) -> list[tuple[int, int]]:
    return [(c.args[1], c.args[2]) for c in mock_controller.write_single_register.await_args_list]


@pytest.mark.asyncio
async def test_y_axis_home_loads_torque_homing_then_triggers_then_parks(mock_controller):
    motor = home_motor(
        mock_controller,
        motor_name="y_axis",
        brake_output_function=0x24,
        brake_release_delay_ms=0,
    )
    registers = echo_registers(mock_controller, {0x0159: 0x24, 0x016D: 0x07})

    await motor.home()

    log = writes(mock_controller)
    trigger = log.index((0x6002, 0x20))
    # Following-error alarm off for the stall, other bits kept.
    assert log.index((0x016D, 0x03)) < trigger
    for write in TORQUE_HOMING_WRITES:
        assert log.index(write) < trigger
    mock_controller.read_holding_registers.assert_any_call(1, 0x0159)
    assert log.index((0x000F, 1)) < trigger < log.index((0x000F, 0))
    assert registers[0x016D] == 0x07
    assert (0x6002, 0x21) not in log
    assert motor.is_homed() is True
    assert motor._pending_motion is None


@pytest.mark.asyncio
async def test_home_uses_custom_torque_and_seeks_far_end_when_inverted(mock_controller):
    motor = home_motor(
        mock_controller,
        lim=184.5,
        phys_at_zero=184.5,
        phys_at_max=0.0,
        home_torque_pct=45,
        home_torque_ms=350,
    )
    echo_registers(mock_controller, {})

    await motor.home()

    log = writes(mock_controller)
    assert (0x600A, 0x0D) in log
    assert (0x6013, 350) in log
    assert (0x6014, 45) in log


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phys_at_zero", "phys_at_max"),
    [
        pytest.param(0.0, 57.0, id="non-inverted"),
        pytest.param(57.0, 0.0, id="inverted"),
    ],
)
async def test_home_overtravel_covers_the_full_travel_despite_lower_lim(
    mock_controller, phys_at_zero, phys_at_max
):
    motor = home_motor(
        mock_controller,
        lim=57.0,
        lower_lim=15.0,
        phys_at_zero=phys_at_zero,
        phys_at_max=phys_at_max,
    )
    echo_registers(mock_controller, {})

    await motor.home()

    # A start at the far end is 57 mm from the stop: 57 / 80 * 10 * 1.25 = 8.9 -> 9 (0.1 rev),
    # not (57 - 15) / 80 * 10 * 1.25 = 6.6 -> 7, which faults before the stop.
    assert (0x6015, 9) in writes(mock_controller)


@pytest.mark.asyncio
async def test_home_retriggers_drive_homing_on_safety_resume(mock_controller):
    motor = home_motor(mock_controller)
    echo_registers(mock_controller, {})
    pending: list[object] = []

    async def wait(**_kwargs):
        pending.append(motor._pending_motion)
        return MotorStatus.STOPPED

    motor.wait_for_move = AsyncMock(side_effect=wait)

    await motor.home()

    assert pending == [motor._trigger_drive_homing]
    assert motor._pending_motion is None


@pytest.mark.asyncio
async def test_home_refuses_to_trigger_when_drive_rejects_torque_method(mock_controller):
    motor = home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x016D: 0x07})
    echo_write = mock_controller.write_single_register.side_effect

    async def write(unit, address, value):
        # Firmware without method 3 keeps its old Pr8.10.
        if address != 0x600A:
            await echo_write(unit, address, value)

    mock_controller.write_single_register.side_effect = write

    with pytest.raises(MotorConfigError, match="Homing mode readback mismatch"):
        await motor.home()

    assert (0x6002, 0x20) not in writes(mock_controller)
    assert registers[0x016D] == 0x07
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_home_fails_when_the_drive_does_not_zero(mock_controller, monkeypatch):
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    motor = home_motor(mock_controller)
    motor._park = AsyncMock()
    # 2500 pulses at 80 mm/rev = 20 mm.
    registers = echo_registers(mock_controller, {0x016D: 0x07, 0x602D: 2500})

    with pytest.raises(MotorError, match="zero did not take: position=20.000 mm"):
        await motor.home()

    assert motor.is_homed() is False
    assert registers[0x016D] == 0x07
    motor._park.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("tolerance", "homed"), [(None, False), (1.0, True)])
async def test_home_zero_tolerance_is_per_motor(mock_controller, monkeypatch, tolerance, homed):
    # The lb4a-3 door read 0.496 mm after a good torque home: it springs back off the stop.
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    overrides = {} if tolerance is None else {"home_zero_tolerance_mm": tolerance}
    motor = home_motor(mock_controller, **overrides)
    motor._park = AsyncMock()
    # 62 pulses at 80 mm/rev = 0.496 mm.
    echo_registers(mock_controller, {0x016D: 0x07, 0x602D: 62})

    if homed:
        await motor.home()
    else:
        with pytest.raises(MotorError, match="zero did not take: position=0.496 mm"):
            await motor.home()

    assert motor.is_homed() is homed


def mapped_home_motor(mock_controller, phys_at_zero: float, phys_at_max: float) -> Motor:
    """home_motor with the config model's real logical/physical mapping."""
    config = MotorConfig(
        motor_name="z_axis", motor_id=1, Ip=10, rpm=300, dr=0, mpr=80.0, accel=100,
        ret_rpm=200, lim=100.0, phys_at_zero=phys_at_zero, phys_at_max=phys_at_max,
    )
    return home_motor(
        mock_controller,
        phys_at_zero=phys_at_zero,
        phys_at_max=phys_at_max,
        physical_to_logical=config.physical_to_logical,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phys_at_zero", "phys_at_max", "logical_at_drive_zero"),
    [
        pytest.param(100.0, 0.0, 100.0, id="inverted"),
        pytest.param(10.0, 100.0, -100.0 / 9, id="offset"),
    ],
)
async def test_home_checks_the_drive_zero_not_the_logical_position(
    mock_controller, phys_at_zero, phys_at_max, logical_at_drive_zero
):
    motor = mapped_home_motor(mock_controller, phys_at_zero, phys_at_max)
    echo_registers(mock_controller, {0x602C: 0, 0x602D: 0})

    await motor.home()

    assert motor.is_homed() is True
    assert motor._cur_pos == pytest.approx(logical_at_drive_zero)


@pytest.mark.asyncio
async def test_home_fails_when_an_inverted_drive_counter_sits_at_logical_zero(
    mock_controller, monkeypatch
):
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    motor = mapped_home_motor(mock_controller, 100.0, 0.0)
    motor._park = AsyncMock()
    # 12500 pulses at 80 mm/rev = physical 100 mm, which is logical 0.
    echo_registers(mock_controller, {0x602D: 12500})

    with pytest.raises(MotorError, match="zero did not take: position=100.000 mm"):
        await motor.home()

    assert motor.is_homed() is False
    motor._park.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_fails_when_the_drive_position_is_unreadable(mock_controller, monkeypatch):
    monkeypatch.setattr("hardware.motor.ZERO_SETTLE_TIMEOUT_S", 0.0)
    motor = home_motor(mock_controller)
    motor._park = AsyncMock()
    # 0x01000000 pulses is far outside the axis, so get_position falls back to its cache.
    echo_registers(mock_controller, {0x602C: 0x0100, 0x602D: 0})

    with pytest.raises(MotorError, match="zero did not take: position=unreadable"):
        await motor.home()

    assert motor.is_homed() is False
    motor._park.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_fault_reports_the_pr_warning(mock_controller):
    motor = home_motor(mock_controller)
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.FAULT)
    registers = echo_registers(mock_controller, {0x016D: 0x07, 0x601D: 0x102})

    with pytest.raises(MotorError, match=r"faulted during homing \(PR warning 0x0102\)"):
        await motor.home()

    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True
    assert registers[0x016D] == 0x07


@pytest.mark.asyncio
async def test_home_timeout_raises(mock_controller):
    motor = home_motor(mock_controller)
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.TIMEOUT)
    echo_registers(mock_controller, {})

    with pytest.raises(MotorTimeoutError, match="homing timed out"):
        await motor.home()

    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_home_fails_when_alarm_detection_cannot_be_restored(mock_controller):
    motor = home_motor(mock_controller)
    registers = echo_registers(mock_controller, {0x016D: 0x07})
    echo_write = mock_controller.write_single_register.side_effect

    async def refuse_restore(unit, address, value):
        if (address, value) != (0x016D, 0x07):
            await echo_write(unit, address, value)

    mock_controller.write_single_register.side_effect = refuse_restore

    with pytest.raises(MotorConfigError, match="alarm detection"):
        await motor.home()

    assert registers[0x016D] == 0x03
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True


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


def direction_recording_motor(mock_controller, **overrides) -> tuple[Motor, list[int]]:
    """A z-like motor whose drive wait records the commanded direction mid-move."""
    params = {
        "motor_name": "z_axis",
        "logical_to_physical": lambda value: value,
        "logical_to_physical_delta": lambda value: value,
        **overrides,
    }
    motor = Motor(mock_controller, make_motor_params(**params))
    seen: list[int] = []

    async def fake_wait_for_move(*args, **kwargs):
        # The weight-change guard reads this while the drive runs the move.
        seen.append(motor.commanded_direction())
        motor._status = MotorStatus.STOPPED

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor.move_config = AsyncMock()
    return motor, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_mm", "target_mm", "expected"),
    [
        pytest.param(60.0, 20.0, -1, id="toward-zero"),
        pytest.param(20.0, 60.0, 1, id="away-from-zero"),
        pytest.param(None, 60.0, 0, id="unknown-start"),
    ],
)
async def test_absolute_move_reports_its_commanded_direction(
    mock_controller, monkeypatch, start_mm, target_mm, expected
):
    monkeypatch.setattr("hardware.motor.asyncio.sleep", AsyncMock())
    motor, seen = direction_recording_motor(mock_controller)
    motor._cur_pos = start_mm

    await motor.move_to_position(target_mm)

    assert seen == [expected]
    # Idle again once the move ends.
    assert motor.commanded_direction() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("dist_mm", "expected"), [(-5.0, -1), (5.0, 1)])
async def test_jog_reports_its_commanded_direction(mock_controller, dist_mm, expected):
    motor, seen = direction_recording_motor(mock_controller)

    await motor.jog(dist_mm)

    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phys_at_zero", "phys_at_max", "expected"),
    [
        pytest.param(0.0, 100.0, -1, id="non-inverted"),
        pytest.param(100.0, 0.0, 1, id="inverted"),
    ],
)
async def test_homing_reports_the_direction_of_the_drive_zero(
    mock_controller, phys_at_zero, phys_at_max, expected
):
    motor = home_motor(mock_controller, phys_at_zero=phys_at_zero, phys_at_max=phys_at_max)
    echo_registers(mock_controller, {})
    seen: list[int] = []

    async def fake_wait_for_move(*args, **kwargs):
        seen.append(motor.commanded_direction())

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor._move_near_home = AsyncMock()

    await motor.home()

    assert seen == [expected]
