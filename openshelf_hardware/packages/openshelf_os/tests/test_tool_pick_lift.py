from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hardware.servo import GripState

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import GripperError
from openshelf_os.tool import Tool

GRIPPED = GripState(0x71, 0x9000, 0x90, 0x40)
MISSED = GripState(0xF1, 0x2000, 0x20, 0x01)


@pytest.mark.asyncio
async def test_pick_lift_place_keeps_z_extended_until_after_release() -> None:
    events: list[str] = []
    coordinate = AxisCoordinate(x=123.0, y=456.0, z_offset=7.5, s=0)
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(side_effect=lambda: events.append("safe_open")),
        open=AsyncMock(side_effect=lambda: events.append("grip") or GRIPPED),
        close=AsyncMock(side_effect=lambda: events.append("release")),
    )
    tool._my = SimpleNamespace(
        move_to_position=AsyncMock(side_effect=lambda y: events.append(f"y:{y}")),
    )
    tool._ms = SimpleNamespace(
        get_position=AsyncMock(return_value=42.0),
        move_to_position=AsyncMock(side_effect=lambda mm: events.append(f"stage:{mm}")),
    )
    tool.move = AsyncMock(side_effect=lambda _: events.append("move"))
    tool.rotate_to_position = AsyncMock(side_effect=lambda _: events.append("rotate"))
    tool.extend_empty = AsyncMock(side_effect=lambda _: events.append("extend"))
    tool.retract_empty = AsyncMock(side_effect=lambda: events.append("retract"))
    tool._handle_fault = AsyncMock()
    sleep = AsyncMock(side_effect=lambda seconds: events.append(f"hold:{seconds}"))

    with patch("openshelf_os.tool.asyncio.sleep", sleep):
        await tool.pick_lift_place(coordinate, lift_mm=12.5, hold_seconds=1.0)

    assert events == [
        "safe_open",
        "move",
        "rotate",
        "extend",
        "grip",
        "stage:0.0",
        "y:468.5",
        "hold:1.0",
        "y:456.0",
        "stage:42.0",
        "release",
        "retract",
    ]
    tool._handle_fault.assert_not_awaited()


@pytest.mark.parametrize("lift_mm", [0.0, -1.0, 30.1])
@pytest.mark.asyncio
async def test_pick_lift_place_rejects_lift_outside_hard_limit(lift_mm: float) -> None:
    tool = Tool.__new__(Tool)

    with pytest.raises(GripperError, match="at most 30 mm"):
        await tool.pick_lift_place(
            AxisCoordinate(x=123.0, y=456.0),
            lift_mm=lift_mm,
            hold_seconds=1.0,
        )


@pytest.mark.asyncio
async def test_pick_lift_place_aborts_before_lift_on_missed_grip() -> None:
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(safe_open=AsyncMock(), open=AsyncMock(return_value=MISSED), close=AsyncMock())
    tool._my = SimpleNamespace(move_to_position=AsyncMock())
    tool._ms = SimpleNamespace(get_position=AsyncMock(), move_to_position=AsyncMock())
    tool.move = tool.rotate_to_position = tool.extend_empty = AsyncMock()
    tool._handle_fault = AsyncMock()

    with pytest.raises(GripperError, match="No grip on grip: obj=NONE"):
        await tool.pick_lift_place(AxisCoordinate(x=1.0, y=2.0), lift_mm=5.0, hold_seconds=0.0)

    tool._my.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.parametrize(("grip", "after_retract", "phase"), [
    (MISSED, GRIPPED, "grip"),
    (GRIPPED, MISSED, "retract"),
])
@pytest.mark.asyncio
async def test_pick_requires_grip_before_and_after_retract(grip, after_retract, phase) -> None:
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(), open=AsyncMock(return_value=grip),
        read_state=AsyncMock(return_value=after_retract),
    )
    tool.move = tool.rotate_to_position = tool.extend_empty = tool.retract = AsyncMock()
    tool._handle_fault = AsyncMock()

    with pytest.raises(GripperError, match=f"No grip on {phase}"):
        await tool.pick(AxisCoordinate(x=1.0, y=2.0))


@pytest.mark.asyncio
async def test_pick_skips_grip_check_when_object_detection_disabled() -> None:
    tool = Tool.__new__(Tool)
    tool.is_grip_detection_enabled = False
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(), open=AsyncMock(return_value=MISSED),
        read_state=AsyncMock(return_value=MISSED),
    )
    tool.move = tool.rotate_to_position = tool.extend_empty = tool.retract = AsyncMock()
    tool._handle_fault = AsyncMock()

    await tool.pick(AxisCoordinate(x=1.0, y=2.0))
    tool._handle_fault.assert_not_awaited()


@pytest.mark.asyncio
async def test_pick_succeeds_when_grip_holds() -> None:
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(), open=AsyncMock(return_value=GRIPPED),
        read_state=AsyncMock(return_value=GRIPPED),
    )
    tool.move = tool.rotate_to_position = tool.extend_empty = tool.retract = AsyncMock()
    tool._handle_fault = AsyncMock()

    await tool.pick(AxisCoordinate(x=1.0, y=2.0))
    tool._handle_fault.assert_not_awaited()
