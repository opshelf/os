"""The tray stays at IO between commands; a return never fetches a shelf tray."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from hardware.exceptions import MotorError as HardwareMotorError
from pydantic import TypeAdapter

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import GripperError, MovementError
from openshelf_os.robot.commands import DeliverTrayCommand, ReturnTrayCommand, RobotCommands, RetrieveItemCommand
from openshelf_os.robot.core import Robot
from openshelf_os.robot.worker import RobotWorker


def build_robot():
    robot = Robot.__new__(Robot)
    robot._operation_context = {}
    operations = Mock()
    tool = SimpleNamespace(**{name: AsyncMock() for name in (
        'pick', 'place', 'rotate_to_position', 'move', 'extend_IO',
        'retract_empty', 'retract', 'clear_alarms',
    )})
    tool.get_param = AsyncMock(return_value=(100, 200))
    tool._gs = SimpleNamespace(open=AsyncMock(), close=AsyncMock())
    io = SimpleNamespace(**{name: AsyncMock() for name in ('open_door', 'close_door', 'home_door')})
    for name, method in vars(tool).items():
        if isinstance(method, AsyncMock):
            operations.attach_mock(method, name)
    for name, method in vars(io).items():
        operations.attach_mock(method, name)
    operations.attach_mock(tool._gs.open, 'grip')
    operations.attach_mock(tool._gs.close, 'release')
    robot._tool = tool
    robot._io = io
    return robot, operations


@pytest.mark.asyncio
async def test_delivery_releases_supported_tray_and_clears_tool_before_door_closes():
    robot, ops = build_robot()
    home = AxisCoordinate(10, 20)
    await robot.deliver_tray(home)
    assert ops.mock_calls == [
        call.get_param('PICKUP_POSITION_L'), call.pick(home),
        call.rotate_to_position('front'), call.move(AxisCoordinate(100, 200)),
        call.open_door(), call.extend_IO(nudge_override=0.0), call.release(),
        call.retract_empty(), call.close_door(), call.home_door(), call.clear_alarms(),
    ]
    robot._tool.place.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_grips_at_same_io_pose_then_returns_to_home_without_shelf_pick():
    robot, ops = build_robot()
    home = AxisCoordinate(10, 20)
    await robot.return_tray(home)
    assert ops.mock_calls == [
        call.get_param('PICKUP_POSITION_L'), call.release(), call.rotate_to_position('front'),
        call.move(AxisCoordinate(100, 200)), call.open_door(),
        call.extend_IO(nudge_override=0.0), call.grip(), call.retract(),
        call.close_door(), call.place(home), call.home_door(), call.clear_alarms(),
    ]
    robot._tool.pick.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['deliver_tray', 'return_tray'])
async def test_failed_extension_does_not_transfer_grip_or_close_door(operation):
    robot, ops = build_robot()
    robot._tool.extend_IO.side_effect = MovementError('interrupted')
    with pytest.raises(MovementError):
        await getattr(robot, operation)(AxisCoordinate(10, 20))
    assert call.grip() not in ops.mock_calls
    assert call.retract() not in ops.mock_calls
    assert call.retract_empty() not in ops.mock_calls
    assert call.close_door() not in ops.mock_calls
    if operation == 'deliver_tray':
        assert call.release() not in ops.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(('operation', 'servo_method', 'phase', 'last_call'), [
    ('deliver_tray', 'close', 'deliver_tray.release', call.release()),
    ('return_tray', 'close', 'return_tray.release', call.release()),
    ('return_tray', 'open', 'return_tray.grip', call.grip()),
])
async def test_tray_servo_fault_preserves_context_and_requires_recovery(
    monkeypatch, operation, servo_method, phase, last_call,
):
    robot, ops = build_robot()
    robot._logger = Mock()
    robot._state = SimpleNamespace(current_cell=None)
    robot._last_fault_context = None
    robot._demo_cell_map = None
    home = AxisCoordinate(10, 20)
    robot._grid = SimpleNamespace(
        get_cell=Mock(return_value=SimpleNamespace(item=None)),
        get_cell_coordinate=Mock(return_value=SimpleNamespace(to_axis_coordinate=lambda: home)),
        clear_item=Mock(),
        store_item=Mock(),
    )
    robot._io.get_weight = AsyncMock(return_value=0.0)
    robot.get_diagnostic_snapshot = AsyncMock(return_value={})
    fault = HardwareMotorError('Servo motion failed')
    getattr(robot._tool._gs, servo_method).side_effect = fault
    location = {'side': 'left_module', 'shelf_idx': 0, 'cell_idx': 2}
    if operation == 'deliver_tray':
        command = DeliverTrayCommand(upca='900000000001', location=location)
    else:
        command = ReturnTrayCommand(item={
            'upca': '900000000001', 'location': location,
            'dimensions': {'width': 1, 'height': 1, 'length': 1},
        })

    task = asyncio.create_task(robot.handle_command(command))
    with pytest.raises(GripperError) as caught:
        await task

    assert caught.value.__cause__ is fault
    assert robot.get_operation_context()['phase'] == phase
    context = robot.get_last_fault_context()['operation_context']
    assert context['phase'] == phase
    assert context['target_cell'] == location
    assert context['target_coordinate']['x'] == home.x
    assert ops.mock_calls[-1] == last_call
    robot._grid.clear_item.assert_not_called()
    robot._grid.store_item.assert_not_called()

    monkeypatch.setattr(RobotWorker, '_instance', None)
    worker = RobotWorker(robot)
    worker._queue_motion_crash_report = Mock()
    await worker._publish_completed_command_result(task, command)

    assert worker.needs_home()
    assert worker._is_crashed
    assert worker._crash_context['operation_context']['phase'] == phase
    error = worker._queue_motion_crash_report.call_args.args[0]
    assert error.error_type == 'HardwareFault'
    assert ops.mock_calls[-1] == last_call


@pytest.mark.parametrize('phase', ['deliver_tray.open_door', 'return_tray.open_door'])
def test_tray_door_opening_arms_existing_light_curtain_guard(phase):
    worker = RobotWorker.__new__(RobotWorker)
    worker._robot = SimpleNamespace(get_operation_context=lambda: {'phase': phase})
    assert worker._is_io_door_opening()


@pytest.mark.asyncio
async def test_internal_commands_dispatch_tray_motion_and_public_payload_cannot_select_it():
    robot = Robot.__new__(Robot)
    robot._operation_context = {}
    robot.store = AsyncMock()
    robot.retrieve_by_location = AsyncMock()
    location = {'side': 'left_module', 'shelf_idx': 0, 'cell_idx': 2}
    deliver = DeliverTrayCommand(upca='900000000001', location=location)
    worker = RobotWorker.__new__(RobotWorker)
    queued = worker._snapshot_command(deliver)
    assert isinstance(queued.command, DeliverTrayCommand)
    await robot.handle_command(queued.command)
    robot.retrieve_by_location.assert_awaited_once_with(location, deliver.upca, tray=True)
    item = {'upca': deliver.upca, 'location': location}
    await robot.handle_command(ReturnTrayCommand(item=item))
    robot.store.assert_awaited_once_with(item, tray=True)
    public = TypeAdapter(RobotCommands).validate_python(deliver.model_dump())
    assert type(public) is RetrieveItemCommand
