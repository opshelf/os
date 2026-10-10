import asyncio
import logging
from types import SimpleNamespace

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.robot import Robot, RobotCommandSuccessful
from openshelf_os.storage import CellItem, Dimensions, StorageModuleCellLocation, StorageModuleSide


def build_robot(
    *, weight: float | None, cached_barcode: str | None = None
) -> tuple[Robot, list[str], list[StorageModuleCellLocation]]:
    calls: list[str] = []

    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )
    placeholder_item = CellItem(
        barcode=cached_barcode,
        dimensions=Dimensions(width=20, height=30, length=400),
        data={"name": "calibration test front_module:0:1"},
    )
    cell = SimpleNamespace(item=placeholder_item)
    cleared_locations: list[StorageModuleCellLocation] = []

    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_robot_retrieve")
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._last_fault_context = None
    robot._demo_cell_map = None
    robot._grid = SimpleNamespace(
        get_cell=lambda resolved_location: cell,
        get_cell_coordinate=lambda resolved_location: SimpleNamespace(
            to_axis_coordinate=lambda: AxisCoordinate(x=10, y=20, s=0, z_offset=1),
        ),
        clear_item=lambda resolved_location: cleared_locations.append(resolved_location),
    )

    async def io_place(coordinate: AxisCoordinate, nudge_override=None) -> None:
        assert coordinate == AxisCoordinate(x=10, y=20, s=0, z_offset=1)
        assert nudge_override is None
        calls.append("io_place")

    async def get_weight() -> float | None:
        calls.append("get_weight")
        return weight

    robot.io_place = io_place
    robot._io = SimpleNamespace(get_weight=get_weight)
    return robot, calls, cleared_locations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cached_barcode",
    # A placeholder with no barcode, and an item the grid still remembers after the
    # cell was reassigned through the API: the commanded barcode wins either way.
    (None, "999900000002"),
)
async def test_retrieve_reports_the_commanded_barcode_over_the_cached_item(
    cached_barcode: str | None,
) -> None:
    robot, _, cleared_locations = build_robot(weight=42.0, cached_barcode=cached_barcode)
    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )

    result = await robot.retrieve_by_location(
        location.model_dump(),
        barcode_hint="999900000001",
    )

    assert isinstance(result, RobotCommandSuccessful)
    assert result.barcode == "999900000001"
    assert result.location == location
    assert cleared_locations == [location]


@pytest.mark.asyncio
async def test_retrieve_weighes_after_io_placement() -> None:
    robot, calls, _ = build_robot(weight=42.0)
    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )

    result = await robot.retrieve_by_location(location.model_dump())

    assert isinstance(result, RobotCommandSuccessful)
    assert result.weight == 42.0
    assert calls == ["io_place", "get_weight"]


@pytest.mark.asyncio
async def test_retrieve_succeeds_without_weight_when_scale_unavailable() -> None:
    robot, _, cleared_locations = build_robot(weight=None)
    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )

    result = await robot.retrieve_by_location(location.model_dump())

    assert isinstance(result, RobotCommandSuccessful)
    assert result.weight is None
    assert cleared_locations == [location]


@pytest.mark.asyncio
async def test_retrieve_cancelled_while_weighing_still_clears_grid_cell() -> None:
    awaiting_weight = asyncio.Event()
    robot, _, cleared_locations = build_robot(weight=42.0)
    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )

    async def get_weight() -> float | None:
        awaiting_weight.set()
        await asyncio.Event().wait()  # simulate the 15s stable-weight wait never returning
        return 42.0

    robot._io = SimpleNamespace(get_weight=get_weight)

    task = asyncio.create_task(robot.retrieve_by_location(location.model_dump()))
    await awaiting_weight.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleared_locations == [location]


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle", ("io_place", "io_pick"))
async def test_io_cycle_homes_the_door_first_and_survives_a_failed_final_home(cycle: str) -> None:
    calls: list[str] = []

    class Tool:
        _my = SimpleNamespace(move_to_position=lambda *_: _record("y_transfer"))

        async def get_param(self, name: str) -> tuple[float, float]:
            return (1.0, 2.0)

        def __getattr__(self, name: str):
            async def step(*_args, **_kwargs) -> None:
                calls.append(name)

            return step

    async def _record(name: str) -> None:
        calls.append(name)

    class Io:
        async def ensure_door_homed(self) -> None:
            calls.append("ensure_door_homed")

        async def open_door(self) -> None:
            calls.append("open_door")

        async def close_door(self) -> None:
            calls.append("close_door")

        async def home_door_after_cycle(self) -> bool:
            calls.append("home_door_after_cycle")
            return False

    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_io_cycle")
    robot._operation_context = {}
    robot._tool = Tool()
    robot._io = Io()

    await getattr(robot, cycle)(AxisCoordinate(x=10, y=20, s=0, z_offset=1))

    assert calls[0] == "ensure_door_homed"
    assert calls.index("ensure_door_homed") < calls.index("pick")
    assert calls[-2:] == ["home_door_after_cycle", "clear_alarms"]
