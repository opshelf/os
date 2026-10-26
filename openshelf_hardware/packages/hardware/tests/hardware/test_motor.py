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
from hardware.exceptions import MotorConfigError, MotorError, MotorLimitError
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
async def test_y_axis_home_lifts_off_the_stop_after_restoring_overrides(mock_controller):
    # lb4a-4: the first climb straight from the homed zero tripped alarm 32 at ~90 mm.
    motor = y_home_motor(mock_controller)
    echo_registers(mock_controller, {0x0159: 0x24})
    targets: list[float] = []
    motor.move_to_position = AsyncMock(side_effect=lambda value, **_: targets.append(value))

    await motor.home(homing_peak_current=30)

    calls = mock_controller.write_single_register.await_args_list
    assert targets == [5.0, 0.0]
    assert motor.is_homed() is True
    # The lift runs at running current and limit, and before the park.
    assert motor.move_to_position.await_count == 2
    assert call(1, 0x0191, 10) in calls and call(1, 0x000B, 4000) in calls
    assert calls.index(call(1, 0x000F, 0)) > calls.index(call(1, 0x000B, 4000))
