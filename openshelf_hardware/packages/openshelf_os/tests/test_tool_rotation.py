from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.exceptions import MotorError
from hardware.motor import Motor, MotorStatus
from openshelf_os.exceptions import MovementError
from openshelf_os.tool import (
    Tool,
)


def _mock_stage_motor(
    limit: float = 50.0, position: float = 0.0, safe_extension_mm: float = 8.5
) -> AsyncMock:
    motor = AsyncMock()
    motor._params = SimpleNamespace(
        lim=limit, rotation_safe_stage_extension_mm=safe_extension_mm
    )
    motor.get_position = AsyncMock(return_value=position)
    motor.move_to_position = AsyncMock()
    return motor


def _mock_x_motor(
    limit: float = 600.0, lower_limit: float = 0.0, position: float = 100.0
) -> AsyncMock:
    motor = AsyncMock()
    motor._params = SimpleNamespace(lim=limit, lower_lim=lower_limit)
    motor.get_position = AsyncMock(return_value=position)
    motor.move_to_position = AsyncMock()
    return motor


@pytest.mark.asyncio
async def test_home_rotate_sequence_homes_unhomed_x_before_clearance_move() -> None:
    events: list[str] = []
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=0.0)
    tool._mx._is_homed = False

    async def home_x() -> None:
        events.append("home_x")
        tool._mx._is_homed = True

    tool._mx.home = AsyncMock(side_effect=home_x)
    tool._mx.move_to_position = AsyncMock(side_effect=lambda mm: events.append(f"x:{mm}"))
    tool._mr = AsyncMock()
    tool._mr.get_position = AsyncMock(return_value=0.0)
    tool._mr.home = AsyncMock(side_effect=lambda: events.append("home_rotate"))
    tool._ms = None
    tool.get_param = AsyncMock(return_value=2.7)
    tool.ensure_safe_rotation_stage_extension = AsyncMock(return_value=False)
    tool.rotate_to_home_orientation = AsyncMock()

    await tool._home_rotate_sequence()

    assert events == ["home_x", "x:200.0", "home_rotate", "x:0.0"]


@pytest.mark.asyncio
async def test_rotate_to_position_restores_original_x_after_safe_edge_move() -> None:
    events: list[str] = []

    x_motor = _mock_x_motor(position=25.0)
    x_position = {"mm": 25.0}

    def didMoveX(position: float) -> None:
        events.append(f"x_move:{position}")
        x_position["mm"] = float(position)

    x_motor.move_to_position = AsyncMock(side_effect=didMoveX)
    x_motor.get_position = AsyncMock(side_effect=lambda: x_position["mm"])

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=0.0)
    rotate_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f"rotate:{position}"))

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    await tool.rotate_to_position("front")

    assert events == [
        "x_move:200.0",
        "rotate:4.0",
        "x_move:25.0",
    ]


@pytest.mark.asyncio
async def test_rotate_to_position_refuses_unsafe_stage_clearance() -> None:
    stage_motor = _mock_stage_motor(position=3.0)
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    with pytest.raises(MovementError, match="stage=3.000mm exceeds maximum 0.500mm"):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotate_to_position_refuses_unsafe_z_clearance() -> None:
    stage_motor = _mock_stage_motor(position=0.0)
    z_motor = AsyncMock()
    z_motor.get_position = AsyncMock(return_value=1.0)
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = None
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    with pytest.raises(MovementError, match="z_axis=1.000mm exceeds maximum 0.500mm"):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


def _tool_for_x_window_check(*, x_mm: float | None, is_homed: bool = True) -> tuple[Tool, AsyncMock]:
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = None
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None if x_mm is None else _mock_x_motor(limit=950.0, position=x_mm)
    if tool._mx is not None:
        tool._mx._is_homed = is_homed
        # The guard is under test, so keep the clearance move from correcting the position first.
        tool.ensure_safe_rotation_x_position = AsyncMock(return_value=None)
        tool.restore_safe_rotation_x_position = AsyncMock()
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    return tool, rotate_motor


@pytest.mark.asyncio
@pytest.mark.parametrize(
	"x_mm, is_homed, error",
	[
		pytest.param(150.0, True, "outside the rotation window 200.0-750.0mm", id="near_min_end"),
		pytest.param(800.0, True, "outside the rotation window 200.0-750.0mm", id="near_max_end"),
		pytest.param(400.0, False, "x_axis is not homed", id="unhomed"),
	],
)
async def test_rotate_to_position_refuses_outside_the_x_window(x_mm: float, is_homed: bool, error: str) -> None:
    tool, rotate_motor = _tool_for_x_window_check(x_mm=x_mm, is_homed=is_homed)

    with pytest.raises(MovementError, match=error):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.asyncio
async def test_retract_fails_when_stage_home_recovery_leaves_stage_unsafe() -> None:
    stage_error = RuntimeError('stage fault')
    stage_motor = _mock_stage_motor(position=6.0)
    stage_motor.clear_alarm = AsyncMock()
    stage_motor.home = AsyncMock()
    stage_motor.idle = AsyncMock()
    stage_motor.move_to_position = AsyncMock(side_effect=stage_error)

    z_motor = AsyncMock()
    z_motor.move_to_position = AsyncMock()
    z_motor.get_position = AsyncMock(return_value=0.0)

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'Z_MOVE_RPM': 100.0,
        'Z_MOVE_ACCEL': 200.0,
    }[name])

    with pytest.raises(MovementError, match='Tool retraction failed'):
        await tool.retract()

    stage_motor.home.assert_awaited_once()
    stage_motor.idle.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.asyncio
async def test_retract_homes_stage_when_zero_move_stops_outside_clearance() -> None:
    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.get_position = AsyncMock(side_effect=[0.75, 0.0])
    stage_motor.clear_alarm = AsyncMock()
    stage_motor.home = AsyncMock()
    stage_motor.idle = AsyncMock()
    z_motor = AsyncMock()
    z_motor.move_to_position = AsyncMock()
    z_motor.get_position = AsyncMock(return_value=0.0)

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        STAGE_RETRACT_POSITION_MM=0.0,
        Z_RETRACT_POSITION_MM=0.0,
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'Z_MOVE_RPM': 100,
        'Z_MOVE_ACCEL': 200,
    }[name])

    await tool.retract()

    stage_motor.clear_alarm.assert_awaited_once()
    stage_motor.home.assert_awaited_once()
    stage_motor.idle.assert_awaited_once()


def _z_axis_motor() -> Motor:
    """A real z_axis Motor, since the incident mechanism lives in homing itself: the recovery
    home repeats the retract move as its pre-home approach. A motor mock cannot show that."""
    params = SimpleNamespace(
        motor_id=30,
        motor_name="z_axis",
        lim=170.0,
        lower_lim=0,
        home_rpm=100,
        ret_rpm=60,
        rpm=100,
        accel=200,
        Ip=10,
        dr=0,
        mpr=80.0,
        phys_at_zero=0.0,
        phys_at_max=170.0,
    )
    controller = Mock()
    controller.write_single_register = AsyncMock()
    controller.read_holding_registers = AsyncMock(return_value=0)
    motor = Motor(controller, params)
    # Real up to the drive: every register write and the homing motion wait are stubbed, so the
    # sequence under test is the one Tool.retract's recovery actually commands.
    motor._on_home_motion_started = None

    for name in ("clear_alarm", "move_config", "idle", "set_param", "get_param",
                 "_prepare_home_motion", "_start_path_zero", "_stop_and_clear_motion",
                 "_set_and_verify_peak_current", "_verify_zero_position",
                 "_arm_following_error_trip", "_clear_homing_trip"):
        setattr(motor, name, AsyncMock())
    motor.wait_for_move = AsyncMock(return_value=MotorStatus.STOPPED)
    return motor


@pytest.mark.asyncio
async def test_retract_recovery_does_not_fail_where_the_retract_move_already_did() -> None:
    """Issue 7708396327: a retract move that faulted the drive partway toward home was followed
    by a recovery home whose pre-home approach commanded that same move again. Homing then died
    with "Failed during pre-home approach" and the whole retraction was lost even though the
    second attempt only needed to cover the ground the first one had already won."""
    motor = _z_axis_motor()
    targets: list[float] = []
    position = [30.0]
    # Each commanded push carries the axis all the way to where it is sent, except the very
    # first one: it gets from 30 mm to 12 mm toward home before the drive faults.
    motor.get_position = AsyncMock(side_effect=lambda: position[0])

    def finish_homing(**kwargs):
        # The homing motion itself finds the stop and zeroes the axis, as it always did.
        position[0] = 0.0

        return MotorStatus.STOPPED

    motor.wait_for_move = AsyncMock(side_effect=finish_homing)

    async def move(value, **kwargs):
        value = float(value)
        targets.append(value)

        if len(targets) == 1:
            position[0] = 12.0
            raise MotorError("Movement failed for motor 30 (status=FAULT)")

        position[0] = value

    motor.move_to_position = AsyncMock(side_effect=move)
    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        STAGE_RETRACT_POSITION_MM=0.0,
        Z_RETRACT_POSITION_MM=0.0,
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = AsyncMock()
    tool._ms.get_position = AsyncMock(return_value=0.0)
    tool._ms.move_to_position = AsyncMock()
    tool._ms.idle = AsyncMock()
    tool._mg = motor
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        "Z_MOVE_RPM": 100,
        "Z_MOVE_ACCEL": 200,
    }[name])

    await tool.retract()

    # Retracted and idle instead of MovementError("Failed during pre-home approach").
    # The retractions first push to 0 mm is the one that faulted; the recovery home then sends
    # its pre-home approach once, to 5 mm short of home, and homes from there. A second 5 mm
    # approach here is the incident: the same faulted move commanded again.
    assert targets == [0.0, 5.0]
    assert tool._handle_fault.await_count == 0
