from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import MovementError, RotationClearanceError
from openshelf_os.tool import (
    Tool,
)


@pytest.mark.asyncio
async def test_is_rotated_back_compares_motor_to_configured_back_angle() -> None:
    tool = Tool.__new__(Tool)
    tool.get_param = AsyncMock(return_value=184.0)
    tool.is_rotation_at_position = AsyncMock(return_value=True)

    assert await tool.is_rotated_back() is True

    tool.get_param.assert_awaited_once_with("ROTATION_BACK")
    tool.is_rotation_at_position.assert_awaited_once_with(184.0)


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
async def test_home_rotate_sequence_rehomes_x_verified_when_its_home_is_provisional() -> None:
    # Isolated init left X provisionally homed after rotate failed; a rotate-only home must verify it.
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=300.0)
    tool._mx._is_homed = True
    tool._mx._is_home_provisional = True
    tool._mr = AsyncMock()
    tool._ms = None
    tool.ensure_safe_rotation_stage_extension = AsyncMock(return_value=False)
    tool.rotate_to_home_orientation = AsyncMock()

    await tool._home_rotate_sequence()

    tool._mx.home.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_home_rotate_sequence_homes_unhomed_x_before_clearance_move() -> None:
    events: list[str] = []
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=0.0)
    tool._mx._is_homed = False
    tool._mx._is_home_provisional = False

    async def home_x(**kwargs) -> None:
        events.append("home_x:provisional" if kwargs.get("provisional") else "home_x")
        tool._mx._is_homed = True
        tool._mx._is_home_provisional = kwargs.get("provisional", False)

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

    assert events == ["home_x:provisional", "x:200.0", "home_rotate", "x:0.0", "home_x"]


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
@pytest.mark.parametrize(
    "position_mm, stage_extension_enabled, error",
    [
        pytest.param(3.0, False, "stage=3.000mm exceeds maximum 0.500mm", id="retract_interlock"),
        # The rotation-safe extension guard must not let a stage still out in a cell turn.
        pytest.param(30.0, True, "stage=30.000mm exceeds maximum 8.600mm", id="beyond_safe_extension"),
    ],
)
async def test_rotate_to_position_refuses_unsafe_stage_clearance(
    position_mm: float, stage_extension_enabled: bool, error: str
) -> None:
    stage_motor = _mock_stage_motor(position=position_mm)
    stage_motor._params.rotation_safe_stage_extension_enabled = stage_extension_enabled
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

    with pytest.raises(MovementError, match=error):
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


def _tool_for_x_guard(*, rotate_deg: float, rotate_homed: bool = True) -> Tool:
    tool = Tool.__new__(Tool)
    tool._logger = Mock()
    tool._mx = _mock_x_motor(limit=950.0, position=400.0)
    tool._mr = AsyncMock()
    tool._mr._is_homed = rotate_homed
    tool._mr.get_position = AsyncMock(return_value=rotate_deg)
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 2.0,
        'ROTATION_BACK': 182.0,
    }[name])
    return tool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rotate_deg, rotate_homed, target_mm, is_refused",
    [
        pytest.param(20.0, True, 10.0, True, id="rotated_toward_end"),
        pytest.param(20.0, True, None, True, id="rotated_home"),
        pytest.param(20.0, True, 400.0, False, id="rotated_inside_window"),
        pytest.param(4.0, True, 10.0, False, id="near_front"),
        pytest.param(180.0, True, None, False, id="near_back"),
        pytest.param(20.0, False, None, False, id="rotate_angle_unknown"),
    ],
)
async def test_x_guard_needs_front_or_back_outside_the_rotation_window(
    rotate_deg: float, rotate_homed: bool, target_mm: float | None, is_refused: bool
) -> None:
    tool = _tool_for_x_guard(rotate_deg=rotate_deg, rotate_homed=rotate_homed)

    if is_refused:
        with pytest.raises(RotationClearanceError, match="rotated to 20.00 deg"):
            await tool._assert_rotation_allows_x_motion(target_mm)
    else:
        await tool._assert_rotation_allows_x_motion(target_mm)


@pytest.mark.asyncio
async def test_restore_leaves_x_in_the_window_while_the_tool_is_off_its_pose() -> None:
    # lb4a-3: restoring X to 0.1 mm after a 20 deg rotate move stalled X on a gantry upright.
    tool = _tool_for_x_guard(rotate_deg=20.0)

    await tool.restore_safe_rotation_x_position(0.1)

    tool._mx.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_leaves_x_in_the_window_while_rotate_is_unhomed() -> None:
    # A faulted or timed-out rotate move clears its homed flag; the angle is then unknown.
    tool = _tool_for_x_guard(rotate_deg=60.0, rotate_homed=False)

    await tool.restore_safe_rotation_x_position(0.1)

    tool._mx.move_to_position.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_home_rotate_sequence_refuses_extended_z_before_any_x_motion() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=0.0)
    tool._mx._is_homed = False
    tool._mx._is_home_provisional = False
    tool._mg = AsyncMock()
    tool._mg.get_position = AsyncMock(return_value=120.0)
    tool._mr = AsyncMock()
    tool._ms = None
    tool.get_param = AsyncMock(return_value=2.7)

    with pytest.raises(RotationClearanceError, match="z_axis"):
        await tool._home_rotate_sequence()

    tool._mx.home.assert_not_awaited()
    tool._mx.move_to_position.assert_not_awaited()
    tool._mr.home.assert_not_awaited()


def _tool_for_place(
    *, x_mm: float, rotate_deg: float = 4.0, stage_extension_enabled: bool = False, extended_mm: float = 0.0
) -> tuple[Tool, list[str]]:
    """A tool on a 0-600 mm X axis (rotation window 200-400 mm) whose motions append to `events`."""
    events: list[str] = []
    axes = {"x": x_mm, "rotate": rotate_deg, "stage": extended_mm, "z": extended_mm}

    def motor(axis: str, *, log_moves: bool = True) -> AsyncMock:
        m = AsyncMock()
        m._is_homed = True
        m.get_position = AsyncMock(side_effect=lambda: axes[axis])

        async def move_to_position(mm: float, **_: object) -> None:
            if log_moves:
                events.append(f"{axis}:{mm}")
            axes[axis] = float(mm)

        m.move_to_position = AsyncMock(side_effect=move_to_position)
        return m

    tool = Tool.__new__(Tool)
    tool._logger = Mock()
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._mx = motor("x")
    tool._mx._params = SimpleNamespace(lim=600.0, lower_lim=0.0)
    tool._mr = motor("rotate")
    tool._ms = motor("stage", log_moves=False)
    tool._ms._params = SimpleNamespace(
        lim=50.0,
        rotation_safe_stage_extension_enabled=stage_extension_enabled,
        rotation_safe_stage_extension_mm=0.0,
    )
    tool._mg = motor("z", log_moves=False)
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {"ROTATION_FRONT": 4.0, "ROTATION_BACK": 184.0}[name])

    async def retract_empty() -> None:
        events.append("retract")
        axes["stage"] = axes["z"] = 0.0

    async def move(coordinate) -> None:
        await tool._ensure_retracted()
        events.append(f"move:{coordinate.x}")
        axes["x"] = coordinate.x

    tool.retract_empty = AsyncMock(side_effect=retract_empty)
    tool.move = AsyncMock(side_effect=move)
    tool.extend = AsyncMock()
    tool.release = AsyncMock()
    tool.is_grip_detection_enabled = False
    tool._gs = SimpleNamespace(read_state=AsyncMock())
    return tool, events


@pytest.mark.asyncio
async def test_place_turns_before_the_move_when_x_already_clears_the_uprights() -> None:
    # The tool is still in its last cell; rotating there first must retract it, or the
    # clearance check would fault where the old move-then-rotate order succeeded.
    tool, events = _tool_for_place(x_mm=300.0, extended_mm=10.0)

    await tool.place(AxisCoordinate(x=550.0, y=100.0, s=1))

    assert events == ["retract", "rotate:184.0", "move:550.0", "retract"]
    tool._handle_fault.assert_not_awaited()


@pytest.mark.asyncio
async def test_place_turns_at_the_nearest_window_edge_instead_of_returning_from_the_target() -> None:
    tool, events = _tool_for_place(x_mm=520.0)

    await tool.place(AxisCoordinate(x=550.0, y=100.0, s=1))

    assert events == ["x:400.0", "rotate:184.0", "move:550.0", "retract"]


@pytest.mark.asyncio
async def test_place_keeps_move_then_rotate_where_rotation_extends_the_stage() -> None:
    # The stage reaches into the cell while turning, so its door must open first, at the cell.
    tool, events = _tool_for_place(x_mm=300.0, stage_extension_enabled=True)

    await tool.place(AxisCoordinate(x=550.0, y=100.0, s=1), before_approach=AsyncMock(
        side_effect=lambda: events.append("open_door")
    ))

    assert events == ["move:550.0", "open_door", "x:400.0", "rotate:184.0", "x:550.0", "retract"]


@pytest.mark.asyncio
async def test_place_skips_rotation_and_its_x_clearance_moves_when_already_turned() -> None:
    tool, events = _tool_for_place(x_mm=520.0, rotate_deg=184.0)

    await tool.place(AxisCoordinate(x=550.0, y=100.0, s=1))

    assert events == ["move:550.0", "retract"]


@pytest.mark.asyncio
async def test_gripper_probe_turns_in_the_window_before_moving_to_a_low_x_cell(monkeypatch) -> None:
    # Gripper open calibration: from a front cell inside the window to a back cell at low X.
    from openshelf_os.laser_calibration import grip_edge

    tool, events = _tool_for_place(x_mm=300.0)
    tool._gs.safe_open = AsyncMock()
    monkeypatch.setattr(grip_edge, "probe_grip_at", AsyncMock(return_value="gripped"))

    await grip_edge.probe_cell(tool, AxisCoordinate(x=50.0, y=100.0, s=1, z_offset=12.0))

    assert events == ["rotate:184.0", "move:50.0"]
    tool._handle_fault.assert_not_awaited()


@pytest.mark.asyncio
async def test_gripper_probe_turns_at_the_window_edge_nearest_a_low_x_start(monkeypatch) -> None:
    from openshelf_os.laser_calibration import grip_edge

    tool, events = _tool_for_place(x_mm=60.0)
    tool._gs.safe_open = AsyncMock()
    monkeypatch.setattr(grip_edge, "probe_grip_at", AsyncMock(return_value="gripped"))

    await grip_edge.probe_cell(tool, AxisCoordinate(x=50.0, y=100.0, s=1, z_offset=12.0))

    assert events == ["x:200.0", "rotate:184.0", "move:50.0"]


@pytest.mark.asyncio
async def test_back_scan_entry_turns_at_the_scan_x_instead_of_a_fixed_mid_rail_stop(monkeypatch) -> None:
    from openshelf_os.laser_calibration import motion
    from openshelf_os.storage import StorageModuleSide

    tool, events = _tool_for_place(x_mm=100.0)
    params = {"ROTATION_FRONT": 4.0, "ROTATION_BACK": 184.0, "Z_MOVE_RPM": 100, "Z_MOVE_ACCEL": 1000}
    tool.get_param = AsyncMock(side_effect=lambda name: params.get(name))
    tool.retract = AsyncMock()
    monkeypatch.setattr(motion, "ensure_scan_tool_ready", AsyncMock())
    monkeypatch.setattr(motion, "get_scan_position", AsyncMock(return_value=(300.0, 50.0)))
    monkeypatch.setattr(motion, "get_laser_scan_pose_stage_mm", lambda _config: 0.0)
    monkeypatch.setattr(motion, "get_laser_scan_z_extension_mm", AsyncMock(return_value=0.0))

    await motion.enter_scan_mode(tool, StorageModuleSide.BACK, scan_config=None)

    assert events == ["move:300.0", "rotate:184.0"]


@pytest.mark.asyncio
async def test_unhomed_x_refuses_rotation_as_a_clearance_refusal() -> None:
    # lb4a-4 2026-11-26: after an X crash every manual rotate came back as a generic motor
    # failure, so the operator could not tell that X (not rotate) needed a home.
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(limit=950.0, position=475.0)
    tool._mx._is_homed = False

    with pytest.raises(RotationClearanceError, match="x_axis is not homed"):
        await tool.ensure_safe_rotation_x_position()

    tool._mx.move_to_position.assert_not_awaited()
