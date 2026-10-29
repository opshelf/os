"""A tray return must hold the tray at grip and after retract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from hardware.servo import GripState

from openshelf_os.exceptions import GRIP_IO_BOX_FAILED_CODE
from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import GripperError, GripVerificationError
from openshelf_os.robot.core import Robot
from openshelf_os.tool import require_grip


def build_robot():
    robot = Robot.__new__(Robot)
    robot._operation_context = {}
    operations = Mock()
    tool = SimpleNamespace(**{name: AsyncMock() for name in (
        'pick', 'place', 'rotate_to_position', 'move', 'extend_IO_multi_item',
        'retract_empty', 'retract', 'clear_alarms', 'release',
    )})
    tool.get_param = AsyncMock(return_value=(100, 200))
    tool.is_grip_detection_enabled = True
    gripped = GripState(0x71, 0x9000, 0x90, 0x40)
    tool._gs = SimpleNamespace(
        open=AsyncMock(return_value=gripped), close=AsyncMock(), read_state=AsyncMock(return_value=gripped),
    )
    io = SimpleNamespace(**{name: AsyncMock() for name in ('open_door', 'close_door', 'home_door', 'ensure_door_homed')})
    for name, method in vars(tool).items():
        if isinstance(method, AsyncMock):
            operations.attach_mock(method, name)
    for name, method in vars(io).items():
        operations.attach_mock(method, name)
    operations.attach_mock(tool._gs.open, 'grip')
    robot._tool = tool
    robot._io = io
    return robot, operations


@pytest.mark.asyncio
@pytest.mark.parametrize(('grip_obj', 'retract_obj', 'match'), [
    (0xF1, 0x71, 'Bin grip failed: No grip on bin grip'),
    (0x71, 0xF1, 'No grip on bin retract'),
])
async def test_return_tray_requires_grip_before_and_after_retract(grip_obj, retract_obj, match):
    robot, ops = build_robot()
    robot._tool._gs.open.return_value = GripState(grip_obj, 0, 0, 0)
    robot._tool._gs.read_state.return_value = GripState(retract_obj, 0, 0, 0)
    with pytest.raises(GripperError, match=match) as raised:
        await robot.return_tray(AxisCoordinate(10, 20))
    # The tray is in the IO box here, so the door has to home with the tool.
    assert raised.value.failure_code == GRIP_IO_BOX_FAILED_CODE
    assert call.close_door() not in ops.mock_calls
    robot._tool.place.assert_not_awaited()


@pytest.mark.parametrize(('position', 'is_held'), [(0x18, True), (0x99, False)])
def test_grip_check_rejects_contact_far_short_of_the_open_target(position, is_held):
    """lb4a-3 reads INNER at 0x18 on a tray wall and also at 0x99 with the fingers caught on nothing."""
    state = GripState(0x71, 0, position, 0x80, open_position=0x10)
    if is_held:
        require_grip(state, 'grip')
    else:
        with pytest.raises(GripVerificationError, match='No grip on grip'):
            require_grip(state, 'grip')


@pytest.mark.asyncio
@pytest.mark.parametrize('cycle', ('deliver_tray', 'return_tray'))
async def test_tray_cycle_homes_a_pending_door_before_any_motion(cycle):
    # A door home cut short by a failed cycle leaves the drive in homing mode; the next
    # tray cycle has to finish that home before it moves the tool or opens the door.
    robot, ops = build_robot()
    await getattr(robot, cycle)(AxisCoordinate(10, 20))
    hardware_calls = [c for c in ops.mock_calls if c[0] != 'get_param']
    assert hardware_calls[0] == call.ensure_door_homed()


TOUCHING = GripState(0x71, 0, 0x90, 0x40)
EMPTY = GripState(0xF1, 0, 0x20, 0x01)


@pytest.mark.asyncio
async def test_place_does_not_withdraw_while_the_gripper_still_touches_the_tray():
    from openshelf_os.exceptions import GRIP_RELEASE_FAILED_CODE
    from openshelf_os.tool import Tool

    tool = Tool.__new__(Tool)
    tool.is_grip_detection_enabled = True
    tool._gs = SimpleNamespace(close=AsyncMock(return_value=TOUCHING))
    for name in ('move', 'rotate_to_position', 'extend', 'retract_empty', '_handle_fault'):
        setattr(tool, name, AsyncMock())

    with pytest.raises(GripVerificationError) as raised:
        await tool.place(AxisCoordinate(10, 20))

    assert raised.value.failure_code == GRIP_RELEASE_FAILED_CODE
    # Withdrawing now would drag the tray out of its cell.
    tool.retract_empty.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_tray_stops_in_the_io_box_when_the_release_leaves_contact():
    from openshelf_os.exceptions import GRIP_RELEASE_FAILED_CODE
    from openshelf_os.tool import Tool

    robot, ops = build_robot()
    robot._tool.release = lambda phase: Tool.release(robot._tool, phase)
    robot._tool._gs.close.return_value = TOUCHING

    with pytest.raises(GripperError) as raised:
        await robot.deliver_tray(AxisCoordinate(10, 20))

    assert raised.value.failure_code == GRIP_RELEASE_FAILED_CODE
    robot._tool.retract_empty.assert_not_awaited()
    assert call.close_door() not in ops.mock_calls


@pytest.mark.asyncio
async def test_deliver_tray_lost_on_the_way_stops_before_the_door_opens():
    from openshelf_os.exceptions import GRIP_DROPPED_CODE

    robot, ops = build_robot()
    robot._tool._gs.read_state.return_value = EMPTY

    with pytest.raises(GripperError) as raised:
        await robot.deliver_tray(AxisCoordinate(10, 20))

    assert raised.value.failure_code == GRIP_DROPPED_CODE
    assert call.open_door() not in ops.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('cycle', ('io_place', 'io_pick'))
async def test_io_cycle_left_without_its_tray_stops_before_the_door_closes(cycle):
    robot, ops = build_robot()
    robot._tool._my = SimpleNamespace(move_to_position=AsyncMock())
    robot._tool.extend_IO = robot._tool.extend = AsyncMock()
    robot._tool._gs.read_state.return_value = EMPTY

    with pytest.raises(GripperError) as raised:
        await getattr(robot, cycle)(AxisCoordinate(10, 20))

    # The tray stayed in the IO box, where the closing door would meet it.
    assert raised.value.failure_code == GRIP_IO_BOX_FAILED_CODE
    assert call.close_door() not in ops.mock_calls
    robot._tool.place.assert_not_awaited()


def build_retract_tool(events):
    from openshelf_os.tool import Tool

    class Axis:
        def __init__(self, name):
            self.name = name

        async def move_to_position(self, target, **_kwargs):
            events.append(f"{self.name} start")
            await asyncio.sleep(0.01)
            events.append(f"{self.name} end")

        async def get_position(self):
            return 0.0

        async def idle(self):
            pass

    tool = Tool.__new__(Tool)
    tool._mg = Axis("z")
    tool._ms = Axis("stage")
    tool._params = SimpleNamespace(parameters=SimpleNamespace(Z_MOVE_RPM=800, Z_MOVE_ACCEL=1200))
    return tool


@pytest.mark.asyncio
async def test_loaded_retract_clears_z_before_the_stage_moves():
    # Moving the stage while Z still holds a loaded tray in the shelf or IO box opens a gap
    # under the tray that the item wedges into or falls through.
    events = []
    await build_retract_tool(events).retract()
    assert events == ["z start", "z end", "stage start", "stage end"]


@pytest.mark.asyncio
async def test_loaded_extend_reaches_the_stage_pose_before_z_pushes_in():
    events = []
    tool = build_retract_tool(events)
    await tool._extend_axes(46.0, 160.0, 800, 1200, loaded=True)
    assert events == ["stage start", "stage end", "z start", "z end"]


@pytest.mark.asyncio
@pytest.mark.parametrize(('cycle', 'flags'), [
    # retrieve: the occupied cell's tray carries the item into the IO box and comes back empty
    ('io_place', {'pick': True, 'extend_IO': True, 'retract': False, 'place': False}),
    # store: the available cell's empty tray goes into the IO box and comes back with the item
    ('io_pick', {'pick': False, 'extend': False, 'retract': True, 'place': True}),
])
async def test_io_cycles_stage_z_only_while_a_tray_may_carry_an_item(cycle, flags):
    robot, _ = build_robot()
    robot._tool._my = SimpleNamespace(move_to_position=AsyncMock())
    robot._tool.extend = AsyncMock()
    robot._tool.extend_IO = AsyncMock()
    robot._tool._gs.read_state = AsyncMock(return_value=GripState(0x71, 0x9000, 0x90, 0x40))
    robot._io.home_door_after_cycle = AsyncMock()
    await getattr(robot, cycle)(AxisCoordinate(10, 20))
    for step, loaded in flags.items():
        assert getattr(robot._tool, step).await_args.kwargs["loaded"] is loaded, step
