import asyncio
import copy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from hardware.servo import GripState

import openshelf_os.fridge as fridge_module
from openshelf_os.common import AxisCoordinate, RootConfig
from openshelf_os.config.schema import CURRENT_SCHEMA_VERSION
from openshelf_os.config.settings import DemoFridgeSettings
from openshelf_os.exceptions import ConfigError, MotorError
from openshelf_os.fridge import DemoFridge, FridgeSafetyError
from openshelf_os.robot.commands import HomeCommand
from openshelf_os.robot.core import Robot
from openshelf_os.robot.worker import RobotWorker
from openshelf_os.storage import StorageModuleCellLocation, StorageModuleSide
from openshelf_os.tool import Tool


GRIPPED = GripState(0x71, 0x9000, 0x90, 0x40)


def motor_config(name: str, motor_id: int, *, limit: float) -> dict[str, object]:
    return {
        "motor_name": name,
        "motor_id": motor_id,
        "Ip": 36,
        "rpm": 500,
        "dr": 0,
        "mpr": 80,
        "accel": 500,
        "ret_rpm": 64,
        "lim": limit,
    }


def config_dict() -> dict[str, object]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "name": "fridge-test",
        "version": "0.1.0",
        "robot": {
            "tool": {
                "parameters": {
                    "IDLE_POSITION": [0, 0],
                    "PICKUP_POSITION_L": [0, 0],
                    "PICKUP_POSITION_H": [0, 0],
                    "SCAN_POSITION": [0, 0],
                    "STAGE_EXTENSION_IO_BOX": 0,
                    "STAGE_NUDGE": 0,
                    "STAGE_EXTENSION_SHELF_FRONT": 0,
                    "STAGE_EXTENSION_SHELF_BACK": 0,
                    "Z_EXTENSION_PICK": 0,
                    "Z_EXTENSION_PLACE": 0,
                    "Z_EXTENSION_IO": 0,
                    "Z_MOVE_RPM": 1,
                    "Z_MOVE_ACCEL": 1,
                    "ROTATION_FRONT": 0,
                    "ROTATION_BACK": 180,
                },
                "motors": [motor_config("stage", 3, limit=120)],
            },
            "grid": {"path": "storage.json"},
            "io": {"motors": []},
        },
        "auth": {"require_auth": False},
        "demo": {
            "fridge": {
                "enabled": True,
                "motor": motor_config("fridge_door", 20, limit=200),
                "door_positions_mm": {
                    "open_left": 0,
                    "closed": 100,
                    "open_right": 200,
                },
                "cells": [
                    {
                        "location": {
                            "side": "right_module",
                            "shelf_idx": 0,
                            "cell_idx": 1,
                        },
                        "stage_extension_mm": 95,
                    }
                ],
            }
        },
    }


def fridge_settings() -> DemoFridgeSettings:
    return RootConfig.model_validate(config_dict()).demo.fridge


def calibration_fridge_settings() -> DemoFridgeSettings:
    data = copy.deepcopy(config_dict())
    fridge = data["demo"]["fridge"]
    fridge["enabled"] = False
    fridge["motor"] = motor_config("fridge_door", 20, limit=814)
    fridge.pop("door_positions_mm")
    fridge["cells"] = []
    return RootConfig.model_validate(data).demo.fridge


def fridge_location() -> StorageModuleCellLocation:
    return StorageModuleCellLocation(
        side=StorageModuleSide.BACK,
        shelf_idx=0,
        cell_idx=1,
    )


def test_demo_fridge_config_accepts_explicit_positions_and_cell() -> None:
    fridge = fridge_settings()

    assert fridge.motor is not None
    assert fridge.motor.motor_id == 20
    assert fridge.door_positions_mm is not None
    assert fridge.door_positions_mm.closed == pytest.approx(100)
    assert fridge.cells[0].stage_extension_mm == pytest.approx(95)


def test_demo_fridge_config_accepts_calibration_only_motor() -> None:
    fridge = calibration_fridge_settings()

    assert fridge.enabled is False
    assert fridge.motor is not None
    assert fridge.motor.motor_id == 20
    assert fridge.motor.lim == pytest.approx(814)
    assert fridge.door_positions_mm is None
    assert fridge.cells == []


def test_calibration_only_fridge_validates_saved_door_positions() -> None:
    data = copy.deepcopy(config_dict())
    fridge = data["demo"]["fridge"]
    fridge["enabled"] = False
    fridge["cells"] = []
    fridge["door_positions_mm"]["closed"] = 200

    with pytest.raises(ValueError, match="positions must be distinct"):
        RootConfig.model_validate(data)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda data: data["demo"]["fridge"]["motor"].update(motor_name="door"),
            "motor_name must be 'fridge_door'",
        ),
        (
            lambda data: data["demo"]["fridge"]["motor"].update(motor_id=19),
            "motor_id must be 20",
        ),
        (
            lambda data: data["demo"]["fridge"]["motor"].update(enabled=False),
            "motor must be enabled",
        ),
        (
            lambda data: data["demo"]["fridge"]["door_positions_mm"].update(closed=200),
            "positions must be distinct",
        ),
        (
            lambda data: data["demo"]["fridge"]["door_positions_mm"].update(
                open_left=100,
                closed=50,
                open_right=150,
            ),
            "closed must be between",
        ),
        (
            lambda data: data["demo"]["fridge"]["cells"][0].update(stage_extension_mm=121),
            "stage extension",
        ),
    ],
)
def test_demo_fridge_config_rejects_unsafe_values(mutation, error: str) -> None:
    data = copy.deepcopy(config_dict())
    mutation(data)

    with pytest.raises(ValueError, match=error):
        RootConfig.model_validate(data)


@pytest.mark.asyncio
async def test_fridge_home_moves_from_end_stop_to_explicit_closed_position(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.home_door()

    door.home.assert_awaited_once()
    door.move_to_position.assert_awaited_once_with(100, rpm=500)


@pytest.mark.asyncio
async def test_calibration_only_fridge_configures_without_startup_motion(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        calibration_fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.configure(home_on_init=True)

    door.configure.assert_awaited_once()
    door.home.assert_not_awaited()
    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_calibration_only_fridge_manual_home_moves_to_derived_center(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        calibration_fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.home_door()

    door.home.assert_awaited_once()
    door.move_to_position.assert_awaited_once_with(407, rpm=500)


@pytest.mark.asyncio
async def test_fridge_opens_away_from_cell_side(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
        x_axis_middle_mm=lambda: 100.0,
    )

    await fridge.open_for_cell(fridge_location(), cell_x_mm=20.0)
    door.move_to_position.assert_awaited_once_with(200, rpm=500)

    door.move_to_position.reset_mock()
    await fridge.open_for_cell(fridge_location(), cell_x_mm=180.0)
    door.move_to_position.assert_awaited_once_with(0, rpm=500)


def test_fridge_direction_uses_x_axis_middle(monkeypatch) -> None:
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: AsyncMock())
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
        x_axis_middle_mm=lambda: 475.0,
    )

    assert fridge.direction_for_cell_x(474.9) == "right"
    assert fridge.direction_for_cell_x(475.0) == "left"
    assert fridge.direction_for_cell_x(900.0) == "left"


def test_fridge_direction_requires_x_axis_limits(monkeypatch) -> None:
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: AsyncMock())
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    with pytest.raises(ConfigError, match="X-axis travel limits"):
        fridge.direction_for_cell_x(10.0)


@pytest.mark.asyncio
async def test_fridge_manual_open_uses_named_positions(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.open_door("right")

    door.move_to_position.assert_awaited_once_with(200, rpm=500)


@pytest.mark.asyncio
async def test_fridge_positional_moves_always_use_configured_speed(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.move_door_to_named("center")
    await fridge.open_door("left")
    await fridge.open_door("right")

    assert door.move_to_position.await_args_list == [
        call(100, rpm=500),
        call(0, rpm=500),
        call(200, rpm=500),
    ]


@pytest.mark.asyncio
async def test_fridge_close_uses_configured_speed(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=True),
    )

    await fridge.close_door()

    door.move_to_position.assert_awaited_once_with(100, rpm=500)


@pytest.mark.asyncio
async def test_fridge_retracts_extended_tool_before_moving_when_rotated_back(
    monkeypatch,
) -> None:
    events: list[str] = []
    retracted = False
    door = AsyncMock()

    async def is_fully_retracted() -> bool:
        events.append("check_retracted")
        return retracted

    async def retract_tool() -> None:
        nonlocal retracted
        events.append("retract")
        retracted = True

    async def move_to_position(*_args, **_kwargs) -> None:
        events.append("move_door")

    door.move_to_position.side_effect = move_to_position
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=is_fully_retracted,
        is_rotated_back=AsyncMock(return_value=True),
        retract_tool=retract_tool,
    )

    await fridge.move_door_to_named("right")

    assert events == ["check_retracted", "retract", "check_retracted", "move_door"]
    door.move_to_position.assert_awaited_once_with(200, rpm=500)


@pytest.mark.asyncio
async def test_fridge_does_not_retract_tool_before_moving_when_not_rotated_back(
    monkeypatch,
) -> None:
    door = AsyncMock()
    is_fully_retracted = AsyncMock(return_value=False)
    retract_tool = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=is_fully_retracted,
        is_rotated_back=AsyncMock(return_value=False),
        retract_tool=retract_tool,
    )

    await fridge.move_door_to_named("left")

    is_fully_retracted.assert_not_awaited()
    retract_tool.assert_not_awaited()
    door.move_to_position.assert_awaited_once_with(0, rpm=500)


@pytest.mark.asyncio
async def test_fridge_does_not_move_when_tool_rotation_cannot_be_verified(
    monkeypatch,
) -> None:
    door = AsyncMock()
    is_fully_retracted = AsyncMock(return_value=False)
    retract_tool = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=is_fully_retracted,
        is_rotated_back=AsyncMock(side_effect=RuntimeError("rotation unavailable")),
        retract_tool=retract_tool,
    )

    with pytest.raises(FridgeSafetyError, match="Cannot verify tool rotation"):
        await fridge.move_door_to_named("left")

    is_fully_retracted.assert_not_awaited()
    retract_tool.assert_not_awaited()
    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fridge_does_not_move_when_back_facing_retraction_cannot_be_verified(
    monkeypatch,
) -> None:
    door = AsyncMock()
    is_fully_retracted = AsyncMock(side_effect=[False, False])
    retract_tool = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=is_fully_retracted,
        is_rotated_back=AsyncMock(return_value=True),
        retract_tool=retract_tool,
    )

    with pytest.raises(FridgeSafetyError, match="fully retracted"):
        await fridge.home_door()

    retract_tool.assert_awaited_once()
    door.home.assert_not_awaited()
    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["home_door", "open_door", "close_door"])
async def test_fridge_does_not_move_door_when_tool_retraction_is_unverified(
    monkeypatch,
    method: str,
) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=False),
    )

    with pytest.raises(FridgeSafetyError, match="fully retracted") as error:
        if method == "open_door":
            await getattr(fridge, method)("left")
        else:
            await getattr(fridge, method)()

    assert isinstance(error.value, MotorError) is False
    door.home.assert_not_awaited()
    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fridge_does_not_close_when_tool_retraction_cannot_be_verified(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(side_effect=RuntimeError("tool telemetry unavailable")),
    )

    with pytest.raises(FridgeSafetyError, match="Cannot verify tool retraction"):
        await fridge.close_door()

    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_home_safety_rejection_does_not_mark_hardware_fault(monkeypatch) -> None:
    monkeypatch.setattr(RobotWorker, "_instance", None)
    robot = Robot.__new__(Robot)
    robot._tool = SimpleNamespace(home=AsyncMock())
    robot._io = SimpleNamespace(home_door=AsyncMock())
    robot._fridge = SimpleNamespace(
        home_door=AsyncMock(
            side_effect=FridgeSafetyError(
                "Tool must be fully retracted before moving fridge door"
            )
        )
    )
    robot._operation_context = {}
    robot._logger = Mock()
    worker = RobotWorker(robot)

    task = asyncio.create_task(robot.handle_command(HomeCommand()))
    with pytest.raises(FridgeSafetyError):
        await task
    await worker._publish_completed_command_result(task, HomeCommand())

    assert worker.needs_home() is False
    assert worker._is_crashed is False


@pytest.mark.asyncio
async def test_isolated_fridge_configure_reports_guard_failure_without_door_motion(monkeypatch) -> None:
    door = AsyncMock()
    reports: list[tuple[str, str, str, str | None]] = []
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=AsyncMock(return_value=False),
    )
    fridge._init_reporter = lambda *args: reports.append(args)

    await fridge.configure(isolate=True)

    door.configure.assert_awaited_once()
    door.home.assert_not_awaited()
    door.move_to_position.assert_not_awaited()
    assert reports[-1][:2] == ("fridge_door", "failed")
    assert reports[-1][3] is not None
    assert "fully retracted" in reports[-1][3]


def robot_with_fridge_access(*, retracted: bool = True):
    location = fridge_location()
    cell = SimpleNamespace(stage_extension_mm=95)
    fridge = SimpleNamespace(
        cell_settings=lambda candidate: cell if candidate == location else None,
        direction_for_cell_x=lambda _x: "left",
        open_for_cell=AsyncMock(),
        close_door=AsyncMock(),
    )
    tool = SimpleNamespace(
        pick=AsyncMock(),
        place=AsyncMock(),
        is_fully_retracted=AsyncMock(return_value=retracted),
    )
    robot = Robot.__new__(Robot)
    robot._fridge = fridge
    robot._tool = tool
    robot._logger = logging.getLogger("test_demo_fridge")
    robot._operation_context = {}
    return robot, tool, fridge, location


@pytest.mark.asyncio
async def test_fridge_access_opens_before_approach_and_closes_after_retraction() -> None:
    robot, tool, fridge, location = robot_with_fridge_access()
    events: list[str] = []

    async def pick(_coord, **kwargs) -> None:
        events.append("approach")
        await kwargs["before_approach"]()
        events.append(f"extend:{kwargs['stage_extension_mm']}")
        events.append("retract")
        await kwargs["after_retract"]()

    tool.pick.side_effect = pick
    fridge.open_for_cell.side_effect = lambda *_args, **_kwargs: events.append("open")
    fridge.close_door.side_effect = lambda: events.append("close")

    await robot._access_storage_cell("pick", AxisCoordinate(10, 20, 1), location)

    assert events == ["approach", "open", "extend:95", "retract", "close"]
    fridge.open_for_cell.assert_awaited_once_with(location, cell_x_mm=10)


@pytest.mark.asyncio
async def test_fridge_access_closes_after_failure_only_when_tool_is_retracted() -> None:
    robot, tool, fridge, location = robot_with_fridge_access(retracted=True)

    async def failed_pick(_coord, **kwargs) -> None:
        await kwargs["before_approach"]()
        raise RuntimeError("transfer failed")

    tool.pick.side_effect = failed_pick

    with pytest.raises(RuntimeError, match="transfer failed"):
        await robot._access_storage_cell("pick", AxisCoordinate(10, 20, 1), location)

    fridge.close_door.assert_awaited_once()


@pytest.mark.asyncio
async def test_fridge_access_leaves_door_open_when_retraction_is_unknown() -> None:
    robot, tool, fridge, location = robot_with_fridge_access(retracted=False)

    async def failed_pick(_coord, **kwargs) -> None:
        await kwargs["before_approach"]()
        raise RuntimeError("transfer failed")

    tool.pick.side_effect = failed_pick

    with pytest.raises(RuntimeError, match="transfer failed"):
        await robot._access_storage_cell("pick", AxisCoordinate(10, 20, 1), location)

    fridge.close_door.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_pick_runs_access_callbacks_around_extension_and_retraction() -> None:
    events: list[str] = []
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(side_effect=lambda: events.append("gripper_safe")),
        open=AsyncMock(side_effect=lambda: events.append("grip") or GRIPPED),
        read_state=AsyncMock(return_value=GRIPPED),
    )
    tool.move = AsyncMock(side_effect=lambda _coord: events.append("move"))
    tool.rotate_to_position = AsyncMock(side_effect=lambda _side: events.append("rotate"))
    tool.extend_empty = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("extend"))
    tool.retract = AsyncMock(side_effect=lambda: events.append("retract"))
    tool._handle_fault = AsyncMock()

    async def before_approach() -> None:
        events.append("open_door")

    async def after_retract() -> None:
        events.append("close_door")

    await tool.pick(
        AxisCoordinate(10, 20, 1),
        stage_extension_mm=95,
        before_approach=before_approach,
        after_retract=after_retract,
    )

    assert events == [
        "gripper_safe",
        "move",
        "open_door",
        "rotate",
        "extend",
        "grip",
        "retract",
        "close_door",
    ]
    tool.extend_empty.assert_awaited_once_with(
        AxisCoordinate(10, 20, 1),
        stage_extension_mm=95,
    )


@pytest.mark.asyncio
async def test_tool_place_opens_door_before_rotation_and_extension() -> None:
    events: list[str] = []
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        close=AsyncMock(side_effect=lambda: events.append("grip")),
    )
    tool.move = AsyncMock(side_effect=lambda _coord: events.append("move"))
    tool.rotate_to_position = AsyncMock(side_effect=lambda _side: events.append("rotate"))
    tool.extend = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("extend"))
    tool.retract_empty = AsyncMock(side_effect=lambda: events.append("retract"))
    tool._handle_fault = AsyncMock()

    async def before_approach() -> None:
        events.append("open_door")

    async def after_retract() -> None:
        events.append("close_door")

    await tool.place(
        AxisCoordinate(10, 20, 1),
        stage_extension_mm=95,
        before_approach=before_approach,
        after_retract=after_retract,
    )

    assert events == [
        "move",
        "open_door",
        "rotate",
        "extend",
        "grip",
        "retract",
        "close_door",
    ]


def test_tool_x_axis_travel_middle_uses_configured_limits() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = SimpleNamespace(_params=SimpleNamespace(lower_lim=10.0, lim=910.0))

    assert tool.x_axis_travel_middle_mm() == pytest.approx(460.0)


def test_tool_x_axis_travel_middle_requires_x_axis() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = None

    with pytest.raises(ConfigError, match="X-axis motor"):
        tool.x_axis_travel_middle_mm()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "callback_name"),
    [
        ("pick", "before_approach"),
        ("pick", "after_retract"),
        ("place", "before_approach"),
        ("place", "after_retract"),
    ],
)
async def test_tool_access_callbacks_preserve_fridge_safety_rejections(
    operation: str,
    callback_name: str,
) -> None:
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(),
        open=AsyncMock(return_value=GRIPPED),
        close=AsyncMock(),
        read_state=AsyncMock(return_value=GRIPPED),
    )
    tool.move = AsyncMock()
    tool.rotate_to_position = AsyncMock()
    tool.extend_empty = AsyncMock()
    tool.extend = AsyncMock()
    tool.retract = AsyncMock()
    tool.retract_empty = AsyncMock()
    tool._handle_fault = AsyncMock()

    async def reject_door_motion() -> None:
        raise FridgeSafetyError("Tool must be fully retracted before moving fridge door")

    with pytest.raises(FridgeSafetyError, match="fully retracted"):
        await getattr(tool, operation)(
            AxisCoordinate(10, 20, 1),
            **{callback_name: reject_door_motion},
        )

    tool._handle_fault.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("z_position", "stage_position", "expected"),
    [
        (1.0, 1.0, True),
        (2.0, 2.0, True),
        (2.1, 1.0, False),
        (1.0, 8.5, False),
    ],
)
async def test_tool_retraction_check_uses_both_axes(
    z_position: float,
    stage_position: float,
    expected: bool,
) -> None:
    tool = Tool.__new__(Tool)
    tool._mg = SimpleNamespace(
        is_homed=Mock(return_value=True),
        get_position=AsyncMock(return_value=z_position),
    )
    tool._ms = SimpleNamespace(
        is_homed=Mock(return_value=True),
        get_position=AsyncMock(return_value=stage_position),
    )
    tool._params = SimpleNamespace(parameters=SimpleNamespace())

    assert await tool.is_fully_retracted() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("z_homed", "stage_homed"), [(False, True), (True, False)])
async def test_tool_retraction_check_requires_both_axes_to_be_homed(
    z_homed: bool,
    stage_homed: bool,
) -> None:
    z_position = AsyncMock(return_value=0.0)
    stage_position = AsyncMock(return_value=0.0)
    tool = Tool.__new__(Tool)
    tool._mg = SimpleNamespace(
        is_homed=Mock(return_value=z_homed),
        get_position=z_position,
    )
    tool._ms = SimpleNamespace(
        is_homed=Mock(return_value=stage_homed),
        get_position=stage_position,
    )
    tool._params = SimpleNamespace(parameters=SimpleNamespace())

    assert await tool.is_fully_retracted() is False
    z_position.assert_not_awaited()
    stage_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_retraction_check_fails_closed_when_homed_state_is_unavailable() -> None:
    z_position = AsyncMock(return_value=0.0)
    stage_position = AsyncMock(return_value=0.0)
    tool = Tool.__new__(Tool)
    tool._mg = SimpleNamespace(
        is_homed=Mock(side_effect=RuntimeError("homing state unavailable")),
        get_position=z_position,
    )
    tool._ms = SimpleNamespace(
        is_homed=Mock(return_value=True),
        get_position=stage_position,
    )
    tool._params = SimpleNamespace(parameters=SimpleNamespace())

    assert await tool.is_fully_retracted() is False
    z_position.assert_not_awaited()
    stage_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_io_transfer_accesses_fridge_for_pick_and_carrier_return() -> None:
    location = fridge_location()
    coord = AxisCoordinate(10, 20, 1)
    tool = SimpleNamespace(
        _my=SimpleNamespace(move_to_position=AsyncMock()),
        get_param=AsyncMock(
            side_effect=lambda name: {
                "PICKUP_POSITION_L": (40, 50),
                "PICKUP_POSITION_H": (40, 80),
            }[name]
        ),
        rotate_to_position=AsyncMock(),
        move=AsyncMock(),
        extend_IO=AsyncMock(),
        retract=AsyncMock(),
        clear_alarms=AsyncMock(),
    )
    robot = Robot.__new__(Robot)
    robot._tool = tool
    robot._io = SimpleNamespace(
        ensure_door_homed=AsyncMock(),
        open_door=AsyncMock(),
        close_door=AsyncMock(),
        home_door=AsyncMock(),
        home_door_after_cycle=AsyncMock(),
    )
    robot._operation_context = {}
    robot._access_storage_cell = AsyncMock()

    await robot.io_place(coord, location=location)

    assert [call.args[0] for call in robot._access_storage_cell.await_args_list] == [
        "pick",
        "place",
    ]
    assert all(
        call.args[2] == location
        for call in robot._access_storage_cell.await_args_list
    )


@pytest.mark.asyncio
async def test_home_command_homes_fridge_after_tool_and_io_door() -> None:
    events: list[str] = []
    robot = Robot.__new__(Robot)
    robot._tool = SimpleNamespace(home=AsyncMock(side_effect=lambda: events.append("tool")))
    robot._io = SimpleNamespace(home_door=AsyncMock(side_effect=lambda: events.append("io_door")))
    robot._fridge = SimpleNamespace(
        home_door=AsyncMock(side_effect=lambda: events.append("fridge_door"))
    )
    robot._operation_context = {}

    await robot.handle_command(HomeCommand())

    assert events == ["tool", "io_door", "fridge_door"]


@pytest.mark.asyncio
async def test_home_command_propagates_fridge_retraction_guard_error(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    tool = SimpleNamespace(
        home=AsyncMock(),
        is_fully_retracted=AsyncMock(return_value=False),
    )
    robot = Robot.__new__(Robot)
    robot._tool = tool
    robot._io = SimpleNamespace(home_door=AsyncMock())
    robot._fridge = DemoFridge(
        AsyncMock(),
        fridge_settings(),
        is_fully_retracted=tool.is_fully_retracted,
    )
    robot._operation_context = {}

    with pytest.raises(FridgeSafetyError, match="fully retracted"):
        await robot.handle_command(HomeCommand())

    door.home.assert_not_awaited()
    door.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_configures_fridge_last() -> None:
    events: list[str] = []
    robot = Robot.__new__(Robot)
    motor_bus = object()
    robot._motorbus = motor_bus
    robot._scalebus = motor_bus
    robot._laserbus = motor_bus
    robot._bus_settings = {"motor": {}}
    robot._logger = logging.getLogger("test_demo_fridge")
    robot._gpio = None
    robot._config = SimpleNamespace(
        robot=SimpleNamespace(bus=None),
        get=lambda key, default=None: {
            "robot.io.scale": SimpleNamespace(enabled=False),
            "robot.tool.laser": SimpleNamespace(enabled=False),
        }.get(key, default),
    )
    robot._connect_controller = AsyncMock()
    robot._emit_init = lambda *_args, **_kwargs: None
    robot._get_motor_by_name = lambda _name: None
    robot._tool = SimpleNamespace(
        configure=AsyncMock(side_effect=lambda **_kwargs: events.append("tool"))
    )
    robot._io = SimpleNamespace(
        configure=AsyncMock(side_effect=lambda **_kwargs: events.append("io_door"))
    )
    robot._cabinet = SimpleNamespace(
        configure=AsyncMock(side_effect=lambda: events.append("cabinet"))
    )
    robot._fridge = SimpleNamespace(
        configure=AsyncMock(side_effect=lambda **_kwargs: events.append("fridge_door"))
    )

    await robot.configure(home_on_init=True)

    assert events == ["tool", "io_door", "cabinet", "fridge_door"]


def test_demo_fridge_config_renames_legacy_cell_sides() -> None:
    # lb2-3's demo config predates the front/back rename and still says right_module.
    data = copy.deepcopy(config_dict())
    data["demo"]["fridge"]["cells"][0]["location"]["side"] = "right_module"

    fridge = RootConfig.model_validate(data).demo.fridge

    assert fridge.cells[0].location.side == "back_module"
