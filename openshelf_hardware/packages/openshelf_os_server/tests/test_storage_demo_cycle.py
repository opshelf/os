import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import FridgeSafetyError
from openshelf_os.storage import StorageModuleCellLocation, StorageModuleSide
from openshelf_os.tool import Tool
from openshelf_os_server.routers import demo


def _demo_location(cell_idx: int = 0) -> StorageModuleCellLocation:
    return StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=cell_idx,
    )


@pytest.fixture(autouse=True)
def reset_demo_cycle_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo, "_demo_task", None)
    monkeypatch.setattr(demo, "_stop_event", asyncio.Event())
    monkeypatch.setattr(demo, "_is_running", False)
    monkeypatch.setattr(demo, "_current_cell_index", None)
    monkeypatch.setattr(demo, "_total_cells", None)


@pytest.mark.anyio
async def test_demo_cycle_passes_mapped_location_to_normal_place_and_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _demo_location()
    coord = MagicMock()
    nudge = 12.5
    worker = SimpleNamespace(_demo_cycling=True)

    async def stop_after_pick(*_args, **_kwargs) -> None:
        demo._stop_event.set()

    robot = SimpleNamespace(
        io_place=AsyncMock(),
        io_pick=AsyncMock(side_effect=stop_after_pick),
    )
    worker._robot = robot

    await demo._run_demo_cycle(worker, [(location, coord, nudge)])

    robot.io_place.assert_awaited_once_with(
        coord,
        nudge_override=nudge,
        location=location,
    )
    robot.io_pick.assert_awaited_once_with(coord, location=location)


@pytest.mark.anyio
async def test_demo_cycle_passes_mapped_location_to_stop_after_place_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _demo_location()
    coord = MagicMock()
    worker = SimpleNamespace(_demo_cycling=True)

    async def stop_after_place(*_args, **_kwargs) -> None:
        demo._stop_event.set()

    robot = SimpleNamespace(
        io_place=AsyncMock(side_effect=stop_after_place),
        io_pick=AsyncMock(),
    )
    worker._robot = robot

    await demo._run_demo_cycle(worker, [(location, coord, None)])

    robot.io_place.assert_awaited_once_with(
        coord,
        nudge_override=None,
        location=location,
    )
    robot.io_pick.assert_awaited_once_with(coord, location=location)


@pytest.mark.anyio
async def test_demo_cycle_fridge_safety_rejection_does_not_mark_hardware_fault() -> None:
    location = _demo_location()
    worker = SimpleNamespace(
        _demo_cycling=True,
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(close=AsyncMock())
    tool.move = AsyncMock()
    tool.rotate_to_position = AsyncMock()
    tool.extend = AsyncMock()
    tool.retract_empty = AsyncMock()
    tool._handle_fault = AsyncMock()

    async def reject_door_motion() -> None:
        raise FridgeSafetyError("Tool must be fully retracted before moving fridge door")

    async def io_place(coord, **_kwargs) -> None:
        await tool.place(coord, before_approach=reject_door_motion)

    worker._robot = SimpleNamespace(
        io_place=AsyncMock(side_effect=io_place),
        io_pick=AsyncMock(),
    )

    await demo._run_demo_cycle(worker, [(location, AxisCoordinate(10, 20, 1), None)])

    tool._handle_fault.assert_not_awaited()
    worker.set_needs_home.assert_not_called()
    worker.get_status_snapshot.assert_awaited_once_with(force_refresh=True)
    assert worker._demo_cycling is False
    assert demo._is_running is False
