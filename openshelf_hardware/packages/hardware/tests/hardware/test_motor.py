import asyncio
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
)
from hardware.exceptions import MotorConfigError, MotorError, MotorLimitError, MotorSafetyStopError
from hardware.servo import GripObject, Servo

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
async def test_move_cancelled_after_trigger_write_stops_the_drive(mock_controller):
    # A peer failure cancels a concurrent start in the 0.1 s after TRIGGER, before wait_for_move.
    motor = Motor(mock_controller, make_motor_params(logical_to_physical=lambda value: value))
    motor.set_param = AsyncMock()
    motor._verify_brake_output_before_motion_trigger = AsyncMock()
    motor._enable_for_motion = AsyncMock()
    triggered = asyncio.Event()

    async def write(_unit, address, value, **_kwargs):
        if value == 0x10:
            triggered.set()

    mock_controller.write_single_register.side_effect = write

    task = asyncio.create_task(motor._start_absolute_move(50.0))
    await triggered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_controller.write_single_register.await_args_list[-1] == call(1, 0x6002, 0x40)
    assert motor._pending_motion is None
    assert motor._target_pos is None


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


def echo_registers(mock_controller, registers: dict[int, int]) -> dict[int, int]:
    """Back register reads with a dict that writes update.

    0x6002 is a command register whose read is drive status, so writes to it are not echoed.
    Pr0.07 is mirrored into the 0x1003 enable bit.
    """
    async def write(_unit, address, value, **_kwargs):
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
        seen.append(motor._status is MotorStatus.MOVING)
        await motor.stop()

        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move
    motor.move_config = AsyncMock()
    motor.is_drive_enabled = AsyncMock(return_value=True)
    mock_controller.read_holding_registers.return_value = 0x24

    status = await motor.jog(1.0)

    assert status is MotorStatus.STOPPED
    assert seen == [True]
    assert motor._status is not MotorStatus.MOVING


@pytest.mark.parametrize("busy", [True, False])
def test_motor_reports_bus_busy_from_its_controller_unit(mock_controller, busy):
    # The IO box door refresh skips its read while the drive is held back; a real Motor has
    # to answer this or every door operation raises AttributeError.
    mock_controller.is_unit_busy = Mock(return_value=busy)
    motor = Motor(mock_controller, make_motor_params(motor_id=7))

    assert motor.is_bus_busy() is busy
    mock_controller.is_unit_busy.assert_called_once_with(7)


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
    triggered_before_wait = []

    async def wait_clear():
        triggered_before_wait.append(
            any(c.args[1] == 0x6002 for c in mock_controller.write_single_register.await_args_list)
        )

    motor._safety.wait_clear.side_effect = wait_clear

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

    # A blocked guard is waited out before the drive is triggered, not after it moved.
    assert triggered_before_wait[0] is False


@pytest.mark.asyncio
async def test_home_paused_by_the_safety_guard_fails_instead_of_zeroing_where_it_stopped(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(),
        safety=SimpleNamespace(blocked=True, wait_clear=AsyncMock()),
    )
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor._set_zero_position = AsyncMock()

    async def fake_wait_for_move(*args, **kwargs):
        # The seek stops short of the limit and nothing restarts it once the guard clears.
        await motor._pause_for_safety_if_needed(SimpleNamespace(reset=lambda _t: None))
        return MotorStatus.STOPPED

    motor.wait_for_move = fake_wait_for_move

    # Its own type, so the worker treats it as an owed home rather than a drive fault.
    with pytest.raises(MotorSafetyStopError, match="safety guard"):
        await motor.home()

    motor._set_zero_position.assert_not_awaited()
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_home_drive_alarm_after_a_guard_pause_still_raises_a_drive_fault(mock_controller):
    motor = Motor(
        mock_controller,
        make_motor_params(),
        safety=SimpleNamespace(blocked=True, wait_clear=AsyncMock()),
    )
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor._set_zero_position = AsyncMock()
    motor.stop = AsyncMock()
    motor._dump_flight_recorder = Mock(return_value=None)

    async def fake_wait_for_move(*args, **kwargs):
        # A drive alarm right after the guard clears is a hardware fault, not a safety stop.
        state = MotionWaitState(start_time=0.0, last_movement_time=0.0)
        await motor._pause_for_safety_if_needed(state)
        return await motor._finish_fault(
            True,
            MotionSnapshot(status_word=1, is_complete=False, has_fault_bit=False),
            MotionAlarmState(alarm_code=7, is_active=True),
            MotionFaultState(is_fault=True, stall_detected=False, no_progress=False),
            state,
        )

    motor.wait_for_move = fake_wait_for_move

    with pytest.raises(MotorError, match="faulted during homing") as raised:
        await motor.home()

    assert not isinstance(raised.value, MotorSafetyStopError)
    motor._set_zero_position.assert_not_awaited()
    assert motor.is_hard_stopped() is True


@pytest.mark.asyncio
async def test_home_stopped_on_its_seek_by_the_guard_is_a_safety_stop_and_skips_the_next_approach(mock_controller):
    # lb4a-3: the guard stopped X mid-seek; the drive then read alarm 32 with the fault bit
    # (status word 65), which crashed the robot, and every later home's approach drove X into
    # its limit because the seek had left the counter off.
    motor = Motor(
        mock_controller,
        make_motor_params(),
        safety=SimpleNamespace(blocked=True, wait_clear=AsyncMock()),
    )
    motor.clear_alarm = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor._set_zero_position = AsyncMock()
    motor.stop = AsyncMock()
    motor._dump_flight_recorder = Mock(return_value=None)
    motor._move_to_position = AsyncMock()
    motor.get_position = AsyncMock(return_value=10.2)

    async def fake_wait_for_move(*args, **kwargs):
        state = MotionWaitState(start_time=0.0, last_movement_time=0.0)
        await motor._pause_for_safety_if_needed(state)
        return await motor._finish_fault(
            True,
            MotionSnapshot(status_word=65, is_complete=False, has_fault_bit=True),
            MotionAlarmState(alarm_code=32, is_active=False),
            MotionFaultState(is_fault=True, stall_detected=False, no_progress=False),
            state,
        )

    motor.wait_for_move = fake_wait_for_move

    with pytest.raises(MotorSafetyStopError, match="safety guard"):
        await motor.home()

    motor._set_zero_position.assert_not_awaited()
    assert motor.is_homed() is False
    motor._move_to_position.reset_mock()
    await motor._move_near_home(rpm=100)
    motor._move_to_position.assert_not_awaited()


def y_home_motor(mock_controller, **overrides) -> Motor:
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name="y_axis",
            brake_output_function=0x24,
            brake_release_delay_ms=0,
            physical_to_logical=lambda value: value,
            logical_to_physical=lambda value: value,
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
    # Energise first, sequencing before SO2, DI1 inert last, then commit.
    assert order["enable_on"] < order["release"] < order["so2"]
    assert order["engage"] < order["so2"]
    assert order["speed"] < order["so2"]
    assert order["so2"] < order["si1"] < order["save"]
    # Parked before the save, so the EEPROM never powers the drive up force-enabled.
    assert order["si1"] < calls.index(call(1, 0x000F, 0)) < order["save"]
    mock_controller.read_holding_registers.assert_any_call(1, 0x1901)


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
    motor.set_param = AsyncMock()
    motor._verify_zero_position = AsyncMock()
    motor.move_config = AsyncMock()
    motor._park = AsyncMock()

    with pytest.raises(MotorLimitError) as excinfo:
        await motor.home()

    assert "x_lim_min" in str(excinfo.value)
    assert motor.is_homed() is False
    assert motor.is_hard_stopped() is True
    # The interlock must fail before the axis reports itself homed and parked.
    motor._park.assert_not_awaited()


def _limit_checked_home_motor(mock_controller, guard: AsyncMock) -> Motor:
    limit_input = SimpleNamespace(name="x_lim_min", read=lambda: False)
    motor = Motor(mock_controller, make_motor_params(), home_limit_input=limit_input)
    motor.set_motion_guard(guard)
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.set_param = AsyncMock()
    motor._verify_zero_position = AsyncMock()
    motor.move_config = AsyncMock()
    motor._park = AsyncMock()
    motor._start_absolute_move = AsyncMock()
    return motor


@pytest.mark.asyncio
async def test_motion_guard_refuses_move_and_home_before_any_motion(mock_controller):
    # lb4a-3: X driven home with the tool at 20 deg stalled on a gantry upright.
    guard = AsyncMock(side_effect=RuntimeError("tool rotated"))
    motor = _limit_checked_home_motor(mock_controller, guard)

    with pytest.raises(RuntimeError, match="tool rotated"):
        await motor.move_to_position(10.0)
    with pytest.raises(RuntimeError, match="tool rotated"):
        await motor.home()

    assert guard.await_args_list == [call(10.0), call(None)]
    motor._start_absolute_move.assert_not_awaited()
    motor._move_near_home.assert_not_awaited()


@pytest.mark.asyncio
async def test_provisional_home_of_an_unhomed_axis_skips_the_guard_and_the_limit_check(
    mock_controller, monkeypatch
):
    # Rotate homing needs X coordinates even when the tool angle holds X off its switch;
    # without this an unhomed X and a rotated tool block each other for good.
    monkeypatch.setattr("hardware.motor.HOME_LIMIT_VERIFY_TIMEOUT_S", 0.0)
    guard = AsyncMock(side_effect=RuntimeError("tool rotated"))
    motor = _limit_checked_home_motor(mock_controller, guard)

    await motor.home(provisional=True)

    guard.assert_not_awaited()
    assert motor.is_homed() is True
    assert motor._is_home_provisional is True


@pytest.mark.asyncio
async def test_provisional_home_seeks_from_where_the_axis_stands_without_the_approach(mock_controller):
    # lb4a-4 2026-11-26: after a crash turned the tool, every rotate home ran X's approach to
    # 5 mm, which stalled on the turned tool at 6.07 mm and faulted before the seek could start.
    motor = _limit_checked_home_motor(mock_controller, AsyncMock())
    motor._move_near_home = AsyncMock(side_effect=AssertionError("approach ran"))

    await motor.home(provisional=True)

    motor._move_near_home.assert_not_awaited()
    motor._start_path_zero.assert_awaited_once()
    assert motor.is_homed() is True
    assert motor._is_home_provisional is True


@pytest.mark.asyncio
async def test_provisional_home_of_a_homed_axis_runs_the_guard_and_is_refused(mock_controller):
    # A homed X has coordinates, so a rotated tool must not be allowed to drive it onto a stop.
    guard = AsyncMock(side_effect=RuntimeError("tool rotated"))
    motor = _limit_checked_home_motor(mock_controller, guard)
    motor._is_homed = True

    with pytest.raises(RuntimeError, match="tool rotated"):
        await motor.home(provisional=True)

    guard.assert_awaited_once_with(None)
    motor._move_near_home.assert_not_awaited()


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
async def test_y_axis_home_zeroes_only_after_clearing_the_trip(mock_controller):
    motor = y_home_motor(mock_controller)
    echo_registers(mock_controller, {0x0159: 0x24})
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
async def test_rehome_failing_after_the_zero_write_leaves_the_axis_unhomed(mock_controller):
    # The drive zero is already rewritten, so the previous home's coordinates no longer hold
    # and later moves must not run against them.
    motor = Motor(mock_controller, make_motor_params(motor_name="x_axis"))
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock()
    motor._set_zero_position = AsyncMock()
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor.move_config = AsyncMock(side_effect=MotorError("move config write failed"))
    motor._is_homed = True
    motor._cur_pos = 42.0

    with pytest.raises(MotorError, match="move config write failed"):
        await motor.home()

    motor._set_zero_position.assert_awaited_once()
    assert motor.is_homed() is False
    assert motor._cur_pos is None
    # It may rest on its limit with a bad counter, so the next home seeks without approaching.
    assert motor._should_skip_pre_home_once is True


@pytest.mark.asyncio
async def test_move_after_a_home_failing_in_its_seek_setup_runs_as_an_absolute_move(mock_controller):
    # The failed home left the drive in homing path mode with the home direction (the opposite
    # of the motor's), and the next move wrote only its target and speed, so the drive ran a
    # homing seek instead of the move.
    motor = Motor(mock_controller, make_motor_params(motor_name="x_axis", logical_to_physical=lambda v: v))
    registers = echo_registers(mock_controller, {})
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._start_path_zero = AsyncMock(side_effect=MotorError("trigger write failed"))
    motor._is_homed = True
    motor._cur_pos = 10.0
    path_reg = motor_helpers.get_register_by_name("Motion of Path 0")
    direction_reg = motor_helpers.get_register_by_name("Motor direction")

    with pytest.raises(MotorError, match="Failed to start homing movement"):
        await motor.home()
    assert (registers[path_reg], registers[direction_reg]) == (0x2, 1)

    at_trigger: dict[str, int] = {}
    echo = mock_controller.write_single_register.side_effect

    async def record_trigger(unit, address, value, **kwargs):
        if address == motor_helpers.TRIGGER and value == 0x10:
            at_trigger.update(path=registers.get(path_reg), direction=registers.get(direction_reg))
        await echo(unit, address, value, **kwargs)

    mock_controller.write_single_register.side_effect = record_trigger
    await motor.move_to_position_no_wait(50.0)

    assert at_trigger == {"path": 0x1, "direction": 0}


@pytest.mark.asyncio
async def test_home_trigger_write_failure_stops_the_drive_and_unhomes(mock_controller):
    # A lost reply does not mean the drive missed the trigger: the seek could run on unwatched
    # while the axis still read homed.
    motor = Motor(mock_controller, make_motor_params(motor_name="x_axis"))
    echo_registers(mock_controller, {})
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._is_homed = True
    writes = mock_controller.write_single_register.side_effect

    async def lose_the_trigger_reply(unit, address, value, **kwargs):
        await writes(unit, address, value, **kwargs)
        if address == motor_helpers.TRIGGER and value == 0x10:
            raise MotorError("no response to the trigger write")

    mock_controller.write_single_register.side_effect = lose_the_trigger_reply

    with pytest.raises(MotorError, match="Failed to start homing movement"):
        await motor.home()

    assert call(1, motor_helpers.TRIGGER, 0x40) in mock_controller.write_single_register.await_args_list
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_y_axis_home_lifts_off_the_stop_and_stays_after_restoring_overrides(mock_controller):
    # lb4a-4: the first climb straight from the homed zero tripped alarm 32 at ~90 mm.
    motor = y_home_motor(mock_controller)
    echo_registers(mock_controller, {0x0159: 0x24})
    targets: list[float] = []
    motor._move_to_position = AsyncMock(side_effect=lambda value, **_: targets.append(value))

    await motor.home(homing_peak_current=30)

    calls = mock_controller.write_single_register.await_args_list
    # lb4a-5: returning to 0 after the lift stalled the next move, so Y stays lifted.
    assert targets == [5.0]
    assert motor.is_homed() is True
    # The lift runs at running current and limit, and before the park.
    assert motor._move_to_position.await_count == 1
    assert call(1, 0x0191, 10) in calls and call(1, 0x000B, 4000) in calls
    assert calls.index(call(1, 0x000F, 0)) > calls.index(call(1, 0x000B, 4000))


@pytest.mark.asyncio
async def test_servo_open_position_changes_on_the_running_drive(mock_controller):
    """The open target is preset 1's register with its enable bit; a target at or past safe-open
    (0xC0) would leave the fingers splayed narrower than the release preset and is refused."""
    servo = _configured_servo(mock_controller)

    await servo.set_open_position(0x06)

    mock_controller.write_single_register.assert_awaited_once_with(0x9, 0x03EB, 0x8006)
    assert servo.open_position == 0x06

    with pytest.raises(ValueError):
        await servo.set_open_position(0xC0)
    assert mock_controller.write_single_register.await_count == 1
    assert servo.open_position == 0x06


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("motor_name", "requested", "expected"),
    [("y_axis", 0.0, 2.0), ("y_axis", 1.0, 2.0), ("y_axis", 3.0, 3.0), ("x_axis", 0.5, 0.5)],
)
async def test_absolute_move_keeps_y_at_least_2mm_off_home(
    mock_controller, motor_name, requested, expected
):
    motor = Motor(
        mock_controller,
        make_motor_params(
            motor_name=motor_name,
            lower_lim=0.0,
            physical_to_logical=lambda value: value,
            logical_to_physical=lambda value: value,
        ),
    )
    motor._cur_pos = 50.0
    motor.set_param = AsyncMock()
    motor._verify_brake_output_before_motion_trigger = AsyncMock()
    motor._enable_for_motion = AsyncMock()

    assert await motor._start_absolute_move(requested) is True
    assert motor._target_pos == expected


@pytest.mark.asyncio
async def test_trial_settings_park_then_restore_ip_and_trip_after_a_faulted_move(mock_controller):
    motor = y_home_motor(mock_controller, Ip=60, max_following_error=4000)
    registers = echo_registers(mock_controller, {0x0159: 0x24, 0x0191: 60, 0x000B: 4000, 0x000F: 1, 0x1003: 1 << 1})

    with pytest.raises(MotorError):
        async with motor.trial_drive_settings(peak_current=15, following_error_mm=10.0):
            # Applied with the load on the brake: Pr0.07 off before the current drops.
            assert registers[0x000F] == 0
            assert registers[0x0191] == 15
            assert registers[0x000B] == 500
            registers[0x000F] = 1  # the faulted move left the axis energised
            registers[0x1003] |= 1 << 1
            raise MotorError("Movement failed for motor 1 (status=FAULT)")

    writes = [c.args[1:] for c in mock_controller.write_single_register.await_args_list]
    assert registers[0x0191] == 60
    assert registers[0x000B] == 4000
    assert registers[0x000F] == 0
    # Parked before the configured current returns, so the restore never lands on a load
    # hanging off a drive below holding current.
    assert writes.index((0x0191, 60)) > max(i for i, w in enumerate(writes) if w == (0x000F, 0))

    with pytest.raises(MotorConfigError):
        async with motor.trial_drive_settings(peak_current=61, following_error_mm=10.0):
            pass


def y_descent_motor(mock_controller, registers: dict[int, int] | None = None, **overrides) -> tuple[Motor, dict[int, int]]:
    """A parked y_axis with a descent current whose moves complete against echoed registers."""
    params = {
        "motor_name": "y_axis",
        "brake_output_function": 0x24,
        "brake_release_delay_ms": 0,
        "brake_engagement_delay_ms": 0,
        "Ip": 60,
        "descent_Ip": 30,
        "lim": 1000.0,
        "logical_to_physical": lambda value: value,
        "logical_to_physical_delta": lambda value: value,
        "physical_to_logical": lambda value: value,
        **overrides,
    }
    motor = Motor(mock_controller, make_motor_params(**params))
    regs = echo_registers(mock_controller, {0x0159: 0x24, 0x0191: 60, **(registers or {})})

    async def finish(**_):
        motor._status = MotorStatus.STOPPED
        await motor._park()
        return MotorStatus.STOPPED

    motor.wait_for_move = AsyncMock(side_effect=finish)
    motor.move_config = AsyncMock()
    return motor, regs


def peak_current_writes(mock_controller) -> list[int]:
    return [c.args[2] for c in mock_controller.write_single_register.await_args_list if c.args[1] == 0x0191]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_mm", "target_mm", "expected"),
    [
        pytest.param(700.0, 300.0, 30, id="down-at-descent-current"),
        pytest.param(300.0, 700.0, 60, id="up-at-ip"),
        pytest.param(None, 300.0, 60, id="unknown-start-at-ip"),
    ],
)
async def test_y_move_writes_its_direction_current_before_the_enable(mock_controller, start_mm, target_mm, expected):
    motor, regs = y_descent_motor(mock_controller)
    motor._cur_pos = start_mm

    await motor.move_to_position(target_mm)

    assert peak_current_writes(mock_controller) == [expected]
    assert regs[0x0191] == expected
    calls = mock_controller.write_single_register.await_args_list
    assert calls.index(call(1, 0x0191, expected)) < calls.index(call(1, 0x000F, 1)) < calls.index(call(1, 0x6002, 0x10))


@pytest.mark.asyncio
async def test_y_move_without_descent_current_leaves_pr500_alone(mock_controller):
    motor, _ = y_descent_motor(mock_controller, descent_Ip=None)
    motor._cur_pos = 700.0

    await motor.move_to_position(300.0)

    assert peak_current_writes(mock_controller) == []


@pytest.mark.asyncio
async def test_y_move_is_refused_before_the_enable_when_the_current_does_not_read_back(mock_controller):
    motor, regs = y_descent_motor(mock_controller)
    motor._cur_pos = 700.0
    write = mock_controller.write_single_register.side_effect

    async def drop_peak_current(unit, address, value, **kwargs):
        if address != 0x0191:
            await write(unit, address, value, **kwargs)

    mock_controller.write_single_register.side_effect = drop_peak_current

    with pytest.raises(MotorError, match="peak current readback mismatch"):
        await motor.move_to_position(300.0)

    assert enable_writes(mock_controller) == []
    assert call(1, 0x6002, 0x10) not in mock_controller.write_single_register.await_args_list
    assert regs[0x0191] == 60


@pytest.mark.asyncio
async def test_y_jog_up_after_a_descent_lifts_at_ip(mock_controller):
    motor, regs = y_descent_motor(mock_controller)
    motor._cur_pos = 700.0
    await motor.move_to_position(300.0)

    await motor.jog(10.0)

    assert peak_current_writes(mock_controller) == [30, 60]
    assert regs[0x0191] == 60


@pytest.mark.asyncio
async def test_y_trial_current_is_not_replaced_by_the_direction_current(mock_controller):
    motor, regs = y_descent_motor(mock_controller, registers={0x000B: 4000})
    motor._cur_pos = 700.0

    async with motor.trial_drive_settings(peak_current=15, following_error_mm=5.0):
        await motor.move_to_position(300.0)
        assert regs[0x0191] == 15

    assert peak_current_writes(mock_controller) == [15, 60]


@pytest.mark.asyncio
async def test_y_hold_runs_at_ip_written_while_parked(mock_controller):
    motor, regs = y_descent_motor(mock_controller, registers={0x0191: 30})
    motor._cur_pos = 700.0

    async with motor.hold_enabled():
        await motor.move_to_position(300.0)
        await motor.move_to_position(600.0)

    assert peak_current_writes(mock_controller) == [60]
    calls = mock_controller.write_single_register.await_args_list
    assert calls.index(call(1, 0x0191, 60)) < calls.index(call(1, 0x000F, 1))


@pytest.mark.asyncio
async def test_y_home_seeks_at_the_descent_current_and_lifts_at_ip(mock_controller):
    motor = y_home_motor(mock_controller, Ip=60, descent_Ip=30, max_following_error=4000)
    regs = echo_registers(mock_controller, {0x0159: 0x24, 0x0191: 60})
    lift_currents: list[int] = []
    motor._move_to_position = AsyncMock(side_effect=lambda value, **_: lift_currents.append(regs[0x0191]))
    seek_currents: list[int] = []
    start_path_zero = motor._start_path_zero

    async def record_seek():
        seek_currents.append(regs[0x0191])
        await start_path_zero()

    motor._start_path_zero = record_seek

    await motor.home()

    assert seek_currents == [30]
    assert lift_currents == [60]
    assert regs[0x0191] == 60


@pytest.mark.asyncio
async def test_y_home_seeks_at_home_ip_over_the_descent_current(mock_controller):
    motor = y_home_motor(mock_controller, Ip=60, descent_Ip=30, home_Ip=20, max_following_error=4000)
    regs = echo_registers(mock_controller, {0x0159: 0x24, 0x0191: 60})
    motor._move_to_position = AsyncMock()
    seek_currents: list[int] = []
    start_path_zero = motor._start_path_zero

    async def record_seek():
        seek_currents.append(regs[0x0191])
        await start_path_zero()

    motor._start_path_zero = record_seek

    await motor.home()

    assert seek_currents == [20]
    assert regs[0x0191] == 60


def _stubbed_home_motor(mock_controller, **overrides) -> tuple[Motor, list[tuple[str, int]]]:
    motor = Motor(mock_controller, make_motor_params(**overrides))
    events: list[tuple[str, int]] = []
    motor.clear_alarm = AsyncMock()
    motor._move_near_home = AsyncMock()
    motor._prepare_home_motion = AsyncMock()
    motor._start_path_zero = AsyncMock(side_effect=lambda: events.append(("seek", 0)))
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    motor._set_zero_position = AsyncMock()
    motor.set_param = AsyncMock()
    motor._verify_zero_position = AsyncMock()
    motor.move_config = AsyncMock()
    motor._park = AsyncMock()
    motor._set_and_verify_peak_current = AsyncMock(side_effect=lambda value: events.append(("current", value)))
    return motor, events


@pytest.mark.asyncio
async def test_home_seeks_at_the_configured_home_current_then_restores_ip(mock_controller):
    # lb4a-9: the rotate seek broke its limit flag running into the stop at full Ip.
    motor, events = _stubbed_home_motor(mock_controller, Ip=36, home_Ip=15)

    await motor.home()

    assert events == [("current", 15), ("seek", 0), ("current", 36)]
    assert motor.is_homed() is True


@pytest.mark.asyncio
async def test_home_without_a_home_current_leaves_the_peak_current_alone(mock_controller):
    motor, events = _stubbed_home_motor(mock_controller, Ip=36)

    await motor.home()

    assert events == [("seek", 0)]


@pytest.mark.asyncio
async def test_failed_home_still_restores_ip_after_the_home_current(mock_controller):
    motor, events = _stubbed_home_motor(mock_controller, Ip=36, home_Ip=15)
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.FAULT)
    motor._raise_home_fault = Mock(side_effect=MotorError("alarm 32"))

    with pytest.raises(MotorError):
        await motor.home()

    assert events == [("current", 15), ("seek", 0), ("current", 36)]


def _two_speed_home_motor(mock_controller, *, approach_moved: bool, switch_engaged: bool):
    # Rotate as on lb4a-9: final seek 12 rpm at 1.5 A, fast seek 24 rpm, approach target 5 deg.
    motor, events = _stubbed_home_motor(
        mock_controller, Ip=36, home_Ip=15, home_rpm=12, ret_rpm=36, home_fast_rpm=24, lim=270, mpr=36.0
    )
    motor._move_near_home = AsyncMock(return_value=approach_moved)
    motor._home_limit_input = Mock(read=Mock(return_value=switch_engaged))
    motor._prepare_home_motion = AsyncMock(side_effect=lambda rpm, direction: events.append(("prepare", rpm)))
    motor._zero_at_home = AsyncMock(side_effect=lambda *args: events.append(("zero", 0)))
    motor._move_to_position = AsyncMock(side_effect=lambda target, **kwargs: events.append(("move", target)))
    return motor, events


@pytest.mark.asyncio
async def test_home_from_a_lost_position_finds_the_switch_fast_then_takes_the_final_stop_slow(mock_controller):
    # After a drive power cycle the counter reads 0 anywhere, so there is no approach; a 270 deg
    # seek at 12 rpm took ~37 s, and only the last stop on the flag needs the slow speed.
    motor, events = _two_speed_home_motor(mock_controller, approach_moved=False, switch_engaged=False)

    await motor.home()

    assert events == [
        ("current", 15),
        ("prepare", 24), ("seek", 0), ("zero", 0),
        ("move", 5.0),
        ("prepare", 12), ("seek", 0), ("zero", 0),
        ("current", 36),
    ]
    assert motor.is_homed() is True


@pytest.mark.parametrize(
    ("approach_moved", "switch_engaged"),
    [
        pytest.param(True, False, id="approach-ran"),
        pytest.param(False, True, id="resting-on-the-switch"),
    ],
)
@pytest.mark.asyncio
async def test_routine_home_only_takes_the_slow_stop(mock_controller, approach_moved, switch_engaged):
    # Every API restart homes rotate from rest on its switch; a fast seek there would put the
    # flag through a 24 rpm stop on each restart.
    motor, events = _two_speed_home_motor(mock_controller, approach_moved=approach_moved, switch_engaged=switch_engaged)

    await motor.home()

    assert [e for e in events if e[0] in ("prepare", "move")] == [("prepare", 12)]


@pytest.mark.asyncio
async def test_failed_fast_seek_stops_the_home_without_backing_off(mock_controller):
    motor, events = _two_speed_home_motor(mock_controller, approach_moved=False, switch_engaged=False)
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.FAULT)
    motor._raise_home_fault = Mock(side_effect=MotorError("alarm"))

    with pytest.raises(MotorError):
        await motor.home()

    assert events == [("current", 15), ("prepare", 24), ("seek", 0), ("current", 36)]
    assert motor.is_homed() is False


@pytest.mark.asyncio
async def test_y_electrical_check_after_a_descent_expects_the_descent_current(mock_controller):
    motor, regs = y_descent_motor(mock_controller, registers={0x0003: 2, 0x0001: 10000, 0x0233: 4000})
    motor._cur_pos = 700.0
    await motor.move_to_position(300.0)

    settings = await motor._read_and_verify_motor_electrical_parameters()

    assert settings["Peak current"] == 30


def fail_peak_current_writes(mock_controller) -> dict[str, bool]:
    """Make Pr5.00 writes raise while the returned flag is set."""
    state = {"fail": False}
    write = mock_controller.write_single_register.side_effect

    async def maybe_fail(unit, address, value, **kwargs):
        if state["fail"] and address == 0x0191:
            raise MotorError("Modbus write failed")
        await write(unit, address, value, **kwargs)

    mock_controller.write_single_register.side_effect = maybe_fail
    return state


async def trial_with_failed_restore(motor, regs, fail: dict[str, bool]) -> None:
    with pytest.raises(MotorError, match="Modbus write failed"):
        async with motor.trial_drive_settings(peak_current=13, following_error_mm=5.0):
            fail["fail"] = True
    assert regs[0x0191] == 13
    assert regs[0x000B] == 250
    assert motor._trial_restore_failed is True


@pytest.mark.asyncio
async def test_move_after_a_failed_trial_restore_rewrites_ip_and_the_trip_before_the_trigger(mock_controller):
    motor, regs = y_descent_motor(mock_controller, descent_Ip=None, registers={0x000B: 4000})
    motor._cur_pos = 700.0
    fail = fail_peak_current_writes(mock_controller)
    await trial_with_failed_restore(motor, regs, fail)
    fail["fail"] = False
    mock_controller.write_single_register.reset_mock()

    await motor.move_to_position(300.0)

    assert regs[0x0191] == 60
    assert regs[0x000B] == 4000
    assert motor._trial_restore_failed is False
    calls = mock_controller.write_single_register.await_args_list
    assert calls.index(call(1, 0x0191, 60)) < calls.index(call(1, 0x6002, 0x10))
    assert calls.index(call(1, 0x000B, 4000)) < calls.index(call(1, 0x6002, 0x10))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["move", "home"])
async def test_motion_is_refused_before_any_trigger_when_the_trial_restore_fails_again(mock_controller, entry):
    if entry == "home":
        motor = y_home_motor(mock_controller, Ip=60, max_following_error=4000)
        regs = echo_registers(mock_controller, {0x0159: 0x24, 0x0191: 60, 0x000B: 4000})
        start = motor.home
    else:
        motor, regs = y_descent_motor(mock_controller, descent_Ip=None, registers={0x000B: 4000})
        motor._cur_pos = 700.0

        async def start():
            await motor.move_to_position(300.0)

    fail = fail_peak_current_writes(mock_controller)
    await trial_with_failed_restore(motor, regs, fail)
    mock_controller.write_single_register.reset_mock()

    with pytest.raises(MotorConfigError, match="trial settings could not be restored"):
        await start()

    assert regs[0x0191] == 13
    assert enable_writes(mock_controller) == [0]
    calls = mock_controller.write_single_register.await_args_list
    assert not [c for c in calls if c.args[1] == 0x6002]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["move", "move_same_pos", "move_no_wait", "jog"])
async def test_faulted_motor_refuses_motion_immediately_before_safety_or_bus_io(mock_controller, entry):
    from hardware.curtain import MotionSafetyInterlock

    safety = MotionSafetyInterlock()
    safety.block()
    motor = Motor(mock_controller, make_motor_params(), safety=safety)
    motor._cur_pos = 100.0
    motor.set_fault()
    mock_controller.write_single_register.reset_mock()

    async def start():
        if entry == "move":
            await motor.move_to_position(150.0)
        elif entry == "move_same_pos":
            await motor.move_to_position(100.0)
        elif entry == "move_no_wait":
            await motor.move_to_position_no_wait(150.0)
        else:
            await motor.jog(10.0)

    with pytest.raises(MotorError) as exc_info:
        await asyncio.wait_for(start(), timeout=1.0)

    assert getattr(exc_info.value, "fault_context", None) == {
        "motor_id": 1,
        "simulated": True,
        "status": "FAULT",
    }
    mock_controller.write_single_register.assert_not_awaited()
    mock_controller.read_holding_registers.assert_not_awaited()
