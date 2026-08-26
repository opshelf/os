import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.motor import MotorStatus
from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import MovementError
from openshelf_os.robot.core import Robot
from openshelf_os.robot.worker import RobotWorker
from openshelf_os.storage import StorageModuleCellLocation, StorageModuleSide
from openshelf_os.tool import Tool


class FakeMotor:
    def __init__(self, motor_id: int, status: MotorStatus = MotorStatus.IDLE) -> None:
        self._motor_id = motor_id
        self._cur_pos = float(motor_id)
        self._target_pos = float(motor_id + 10)
        self._status = status
        self._simulated_fault = status == MotorStatus.FAULT
        self._params = SimpleNamespace(lower_lim=0.0, lim=500.0)

    def is_homed(self) -> bool:
        return True

    def is_hard_stopped(self) -> bool:
        return self._status == MotorStatus.FAULT


class FailingMotor(FakeMotor):
    def is_homed(self) -> bool:
        raise RuntimeError("homed unavailable")


def build_robot_for_diagnostics() -> Robot:
    robot = Robot.__new__(Robot)
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._last_fault_context = None
    robot._tool = SimpleNamespace(
        _mx=FakeMotor(1),
        _my=FailingMotor(2),
        _mg=FakeMotor(3),
        _ms=None,
        _mr=FakeMotor(5, MotorStatus.FAULT),
    )
    robot._io = SimpleNamespace(_md=None)
    robot.get_curtain_state = lambda: False
    robot.get_door_state = lambda: None
    return robot


@pytest.mark.asyncio
async def test_diagnostic_snapshot_includes_all_axes_and_survives_read_failures() -> None:
    robot = build_robot_for_diagnostics()

    snapshot = await robot.get_diagnostic_snapshot()

    assert set(snapshot["motors"]) == {"x_axis", "y_axis", "z_axis", "stage", "rotate", "door"}
    assert snapshot["motors"]["x_axis"]["motor_id"] == 1
    assert snapshot["motors"]["rotate"]["status"] == "FAULT"
    assert snapshot["motors"]["stage"]["configured"] is False
    assert snapshot["motors"]["door"]["configured"] is False
    assert snapshot["motors"]["y_axis"]["homed_error"] == "homed unavailable"


@pytest.mark.asyncio
async def test_worker_error_context_contains_operation_diagnostics_and_needs_home_axes() -> None:
    RobotWorker._instance = None
    robot = build_robot_for_diagnostics()
    robot.set_operation_context(command="retrieve_item", phase="io_place.extend_into_io")
    worker = RobotWorker(robot)
    worker.set_needs_home("motor 5 fault")

    context = await worker._build_error_context(MovementError("gantry failed"), is_hardware_fault=True)

    assert context["operation_context"]["command"] == "retrieve_item"
    assert context["operation_context"]["phase"] == "io_place.extend_into_io"
    assert context["diagnostic_snapshot"]["motors"]["rotate"]["status"] == "FAULT"
    assert context["hardware_fault"]["exception_type"] == "MovementError"
    assert context["hardware_fault"]["needs_home_axes"] == ["rotate"]


@pytest.mark.asyncio
async def test_simulated_motor_crash_sets_structured_crash_context_and_error_queue() -> None:
    RobotWorker._instance = None
    robot = build_robot_for_diagnostics()
    robot.simulate_motor_crash = lambda motor_name: True
    worker = RobotWorker(robot)

    async def did_publish_status() -> None:
        return None

    worker._publish_status = did_publish_status

    assert await worker.simulate_motor_crash("x_axis") is True
    assert worker._is_crashed is True
    assert worker._crash_context is not None
    assert worker._crash_context["diagnostic_snapshot"]["motors"]["x_axis"]["motor_id"] == 1
    assert worker._error_queue[-1].context is not None
    assert worker._error_queue[-1].context["hardware_fault"]["needs_home_axes"] == ["x_axis"]


@pytest.mark.asyncio
async def test_store_fault_preserves_resolved_cell_and_coordinate_context() -> None:
    robot = Robot.__new__(Robot)
    robot._logger = SimpleNamespace(info=lambda *args, **kwargs: None, exception=lambda *args, **kwargs: None)
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._last_fault_context = None
    robot._demo_cell_map = None
    robot._reserved_cell_locations = {("left_module", 1, 2)}
    location = StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=1, cell_idx=2)
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=1.5)
    async def did_get_weight() -> float:
        return 42.0

    robot._io = SimpleNamespace(get_weight=did_get_weight)
    robot._grid = SimpleNamespace(
        find_best_slot_for_item=lambda item, *, excluded_locations: location,
        get_cell_coordinate=lambda resolved_location: SimpleNamespace(to_axis_coordinate=lambda: coordinate),
        store_item=lambda resolved_location, item: None,
    )

    async def did_fail_io_pick(coord: AxisCoordinate) -> None:
        raise MovementError("x=FAULT, y=STOPPED")

    robot.io_pick = did_fail_io_pick

    with pytest.raises(MovementError):
        await robot.store({"upca": "123", "name": "test", "dimensions": {"width": 1, "height": 2, "length": 3}})

    fault_context = robot.get_last_fault_context()
    assert fault_context is not None
    operation_context = fault_context["operation_context"]
    assert operation_context["command"] == "store_item"
    assert operation_context["target_cell"]["side"] == "left_module"
    assert operation_context["target_cell"]["shelf_idx"] == 1
    assert operation_context["target_coordinate"]["x"] == 12.0
    assert operation_context["phase"] == "store.moving_from_shelf_to_io"


def build_fault_reporting_tool() -> Tool:
    tool = Tool.__new__(Tool)
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool._mx = SimpleNamespace(
        wait_for_move=lambda **kwargs: MotorStatus.FAULT,
        _motor_id=1,
        _cur_pos=10.0,
        _target_pos=20.0,
        _status=MotorStatus.FAULT,
        _simulated_fault=True,
        is_homed=lambda: False,
        is_hard_stopped=lambda: True,
    )
    tool._my = SimpleNamespace(
        wait_for_move=lambda **kwargs: MotorStatus.STOPPED,
        _motor_id=2,
        _cur_pos=30.0,
        _target_pos=30.0,
        _status=MotorStatus.STOPPED,
        _simulated_fault=False,
        is_homed=lambda: True,
        is_hard_stopped=lambda: False,
    )
    tool._mg = None
    tool._ms = None
    tool._mr = None
    return tool


@pytest.mark.asyncio
async def test_gantry_wait_preserves_axis_status_details_on_fault() -> None:
    tool = build_fault_reporting_tool()

    async def wait_x(**kwargs):
        return MotorStatus.FAULT

    async def wait_y(**kwargs):
        return MotorStatus.STOPPED

    tool._mx.wait_for_move = wait_x
    tool._my.wait_for_move = wait_y

    with pytest.raises(MovementError) as exc_info:
        await tool.gantry_wait(timeout_seconds=0.1)

    assert "x=FAULT, y=STOPPED" in str(exc_info.value)
    fault_context = getattr(exc_info.value, "fault_context")
    assert fault_context["phase"] == "gantry.wait"
    assert fault_context["axis_statuses"] == {"x_axis": "FAULT", "y_axis": "STOPPED"}
    assert fault_context["motors"]["x_axis"]["status"] == "FAULT"


@pytest.mark.asyncio
async def test_move_preserves_wait_axis_status_details_on_fault() -> None:
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)

    async def move_y(position: float) -> None:
        assert position == coordinate.y

    async def move_x(position: float) -> None:
        assert position == coordinate.x

    async def wait_x(**kwargs):
        return MotorStatus.FAULT

    async def wait_y(**kwargs):
        return MotorStatus.STOPPED

    tool._my.move_to_position_no_wait = move_y
    tool._mx.move_to_position_no_wait = move_x
    tool._mx.wait_for_move = wait_x
    tool._my.wait_for_move = wait_y

    with pytest.raises(MovementError) as exc_info:
        await tool.move(coordinate)

    fault_context = getattr(exc_info.value, "fault_context")
    assert fault_context["phase"] == "gantry.move"
    assert fault_context["coordinate"] == {
        "x": coordinate.x,
        "y": coordinate.y,
        "s": coordinate.s,
        "z_offset": coordinate.z_offset,
    }
    assert fault_context["axis_statuses"] == {"x_axis": "FAULT", "y_axis": "STOPPED"}
    assert fault_context["previous_fault_context"]["phase"] == "gantry.wait"
    assert fault_context["previous_fault_context"]["axis_statuses"] == {"x_axis": "FAULT", "y_axis": "STOPPED"}


@pytest.mark.asyncio
async def test_move_cancellation_during_x_start_stops_and_clears_both_gantry_axes() -> None:
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    x_cleanup = AsyncMock()
    y_cleanup = AsyncMock()

    async def move_x(_: float) -> None:
        raise asyncio.CancelledError()

    tool._my.move_to_position_no_wait = AsyncMock()
    tool._mx.move_to_position_no_wait = move_x
    tool._mx._stop_and_clear_motion = x_cleanup
    tool._my._stop_and_clear_motion = y_cleanup

    with pytest.raises(asyncio.CancelledError):
        await tool.move(coordinate)

    tool._my.move_to_position_no_wait.assert_awaited_once_with(coordinate.y)
    x_cleanup.assert_awaited_once_with()
    y_cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_move_cancellation_during_error_cleanup_waits_for_both_gantry_axes() -> None:
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    x_cleanup_started = asyncio.Event()
    y_cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    x_cleanup_finished = asyncio.Event()
    y_cleanup_finished = asyncio.Event()

    async def move_x(_: float) -> None:
        raise RuntimeError("x start failed")

    async def cleanup_x() -> None:
        x_cleanup_started.set()
        await release_cleanup.wait()
        x_cleanup_finished.set()

    async def cleanup_y() -> None:
        y_cleanup_started.set()
        await release_cleanup.wait()
        y_cleanup_finished.set()

    tool._my.move_to_position_no_wait = AsyncMock()
    tool._mx.move_to_position_no_wait = move_x
    tool._mx._stop_and_clear_motion = cleanup_x
    tool._my._stop_and_clear_motion = cleanup_y

    move_task = asyncio.create_task(tool.move(coordinate))
    await x_cleanup_started.wait()
    await y_cleanup_started.wait()
    move_task.cancel()
    await asyncio.sleep(0)
    assert not move_task.done()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await move_task

    assert x_cleanup_finished.is_set()
    assert y_cleanup_finished.is_set()
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert not isinstance(exc_info.value, MovementError)


@pytest.mark.asyncio
async def test_handle_fault_seeds_wrapper_context_from_tool_wait_cause() -> None:
    tool = Tool.__new__(Tool)
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool._mx = None
    tool._my = None
    tool._mr = None
    tool._mg = SimpleNamespace(
        ready=lambda: None,
        move_to_position_no_wait=lambda *args, **kwargs: None,
        wait_for_move=lambda **kwargs: None,
        _motor_id=3,
        _cur_pos=30.0,
        _target_pos=40.0,
        _status=MotorStatus.FAULT,
        _simulated_fault=True,
        is_homed=lambda: False,
        is_hard_stopped=lambda: True,
    )
    tool._ms = SimpleNamespace(
        ready=lambda: None,
        move_to_position_no_wait=lambda *args, **kwargs: None,
        wait_for_move=lambda **kwargs: None,
        _motor_id=4,
        _cur_pos=50.0,
        _target_pos=60.0,
        _status=MotorStatus.STOPPED,
        _simulated_fault=False,
        is_homed=lambda: True,
        is_hard_stopped=lambda: False,
        _params=SimpleNamespace(lim=500.0),
    )

    async def did_get_shelf_slide_extension(coordinate: AxisCoordinate | None) -> float:
        assert coordinate is not None
        return 25.0

    async def did_get_param(name: str) -> float:
        values = {
            "Z_EXTENSION_PICK": 35.0,
            "Z_MOVE_RPM": 100.0,
            "Z_MOVE_ACCEL": 200.0,
        }
        return values[name]

    async def did_ready() -> None:
        return None

    async def did_move_to_position_no_wait(*args, **kwargs) -> None:
        return None

    async def did_wait_z(**kwargs):
        return MotorStatus.FAULT

    async def did_wait_stage(**kwargs):
        return MotorStatus.STOPPED

    tool._get_shelf_slide_extension = did_get_shelf_slide_extension
    tool.get_param = did_get_param
    tool._ms.ready = did_ready
    tool._mg.move_to_position_no_wait = did_move_to_position_no_wait
    tool._ms.move_to_position_no_wait = did_move_to_position_no_wait
    tool._mg.wait_for_move = did_wait_z
    tool._ms.wait_for_move = did_wait_stage

    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=1.5)

    with pytest.raises(MovementError) as exc_info:
        await tool.extend_empty(coordinate)

    wrapped_error = exc_info.value
    assert getattr(wrapped_error, "fault_context", None) is None

    await tool._handle_fault(wrapped_error, phase="tool.extend_empty", coordinate=coordinate)

    fault_context = getattr(wrapped_error, "fault_context")
    assert fault_context["phase"] == "tool.extend_empty"
    assert fault_context["coordinate"] == {
        "x": coordinate.x,
        "y": coordinate.y,
        "s": coordinate.s,
        "z_offset": coordinate.z_offset,
    }
    assert fault_context["axis_statuses"] == {"z_axis": "FAULT", "stage": "STOPPED"}
    assert fault_context["previous_fault_context"]["phase"] == "tool.wait"
    assert fault_context["previous_fault_context"]["axis_statuses"] == {
        "z_axis": "FAULT",
        "stage": "STOPPED",
    }
