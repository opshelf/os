import logging
from types import SimpleNamespace

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.robot import Robot, RobotCommandSuccessful
from openshelf_os.storage import CellItem, Dimensions, StorageModuleCellLocation, StorageModuleSide


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

    robot.io_place = io_place

    result = await robot.retrieve_by_location(
        location.model_dump(),
        barcode_hint="999900000001",
    )

    assert isinstance(result, RobotCommandSuccessful)
    assert result.barcode == "999900000001"
    assert result.location == location
    assert cleared_locations == [location]
