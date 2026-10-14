"""A tray return must hold the tray at grip and after retract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from hardware.servo import GripState

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import GripperError
from openshelf_os.robot.core import Robot


def build_robot():
    robot = Robot.__new__(Robot)
    robot._operation_context = {}
    operations = Mock()
    tool = SimpleNamespace(**{name: AsyncMock() for name in (
        'pick', 'place', 'rotate_to_position', 'move', 'extend_IO_multi_item',
        'retract_empty', 'retract', 'clear_alarms',
    )})
    tool.get_param = AsyncMock(return_value=(100, 200))
    tool.is_grip_detection_enabled = True
    gripped = GripState(0x71, 0x9000, 0x90, 0x40)
    tool._gs = SimpleNamespace(
        open=AsyncMock(return_value=gripped), close=AsyncMock(), read_state=AsyncMock(return_value=gripped),
    )
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
@pytest.mark.parametrize(('grip_obj', 'retract_obj', 'match'), [
    (0xF1, 0x71, 'Tray grip failed: No grip on tray grip'),
    (0x71, 0xF1, 'No grip on tray retract'),
])
async def test_return_tray_requires_grip_before_and_after_retract(grip_obj, retract_obj, match):
    robot, ops = build_robot()
    robot._tool._gs.open.return_value = GripState(grip_obj, 0, 0, 0)
    robot._tool._gs.read_state.return_value = GripState(retract_obj, 0, 0, 0)
    with pytest.raises(GripperError, match=match):
        await robot.return_tray(AxisCoordinate(10, 20))
    assert call.close_door() not in ops.mock_calls
    robot._tool.place.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_tray_skips_grip_check_when_object_detection_disabled():
    robot, ops = build_robot()
    robot._tool.is_grip_detection_enabled = False
    robot._tool._gs.open.return_value = GripState(0xF1, 0, 0, 0)
    robot._tool._gs.read_state.return_value = GripState(0xF1, 0, 0, 0)
    await robot.return_tray(AxisCoordinate(10, 20))
    assert call.close_door() in ops.mock_calls
    robot._tool.place.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['deliver_tray', 'return_tray'])
async def test_tray_transfers_use_the_multi_item_io_pose(method):
    robot, _ = build_robot()
    poses = {'PICKUP_POSITION_L': (100, 200), 'PICKUP_POSITION_MULTI_ITEM': (300, 400)}
    robot._tool.get_param = AsyncMock(side_effect=lambda name: poses.get(name))
    await getattr(robot, method)(AxisCoordinate(10, 20))
    robot._tool.move.assert_any_await(AxisCoordinate(300, 400))
    robot._tool.extend_IO_multi_item.assert_awaited_once_with()
