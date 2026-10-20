from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.robot.core import Robot
from openshelf_os.robot.status import RobotCommandError
from openshelf_os.storage import (
    CalibrationCoordinate,
    Cell,
    CellStatus,
    Dimensions,
    Shelf,
    StorageGrid,
    StorageModuleCellLocation,
    StorageModuleSide,
)


class StoreGrid:
    def __init__(self) -> None:
        self.stored_locations: list[StorageModuleCellLocation] = []

    def get_cell_coordinate(self, location: StorageModuleCellLocation) -> SimpleNamespace:
        return SimpleNamespace(
            to_axis_coordinate=lambda: AxisCoordinate(x=10.0, y=20.0, s=0, z_offset=0.0)
        )

    def store_item(self, location: StorageModuleCellLocation, item: object) -> None:
        self.stored_locations.append(location)


def build_grid(
    *,
    status: CellStatus = CellStatus.AVAILABLE,
    dimensions: Dimensions | None = None,
) -> StorageGrid:
    return StorageGrid(
        front_module=[
            Shelf(
                shelf_idx=0,
                cells=[
                    Cell(
                        cell_idx=0,
                        status=status,
                        item=None,
                        calibration_point=CalibrationCoordinate(x=10.0, y=20.0),
                        dimensions=dimensions
                        or Dimensions(width=100.0, length=100.0, height=100.0),
                    )
                ],
            )
        ],
        back_module=[],
    )


def build_store_robot(
    grid: StoreGrid | StorageGrid,
    demo_cell_map: dict[str, StorageModuleCellLocation] | None = None,
    reserved_locations: set[tuple[str, int, int]] | None = None,
) -> Robot:
    robot = Robot.__new__(Robot)
    robot._logger = SimpleNamespace(info=lambda *args, **kwargs: None, exception=lambda *args, **kwargs: None)
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._demo_cell_map = demo_cell_map
    robot._reserved_cell_locations = reserved_locations or set()
    robot._io = SimpleNamespace(get_weight=AsyncMock(return_value=42.0))
    robot.io_pick = AsyncMock()
    robot._grid = grid
    return robot


def item_payload(barcode: str, location: StorageModuleCellLocation | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "barcode": barcode,
        "name": f"Storage bin {barcode}",
        "dimensions": {"width": 1, "height": 2, "length": 3},
    }
    if location is not None:
        payload["location"] = location.model_dump()
    return payload


@pytest.mark.asyncio
async def test_non_demo_store_rejects_occupied_explicit_location() -> None:
    location = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0)
    robot = build_store_robot(build_grid(status=CellStatus.OCCUPIED))

    result = await robot.store(item_payload("ordinary-item", location))

    assert isinstance(result, RobotCommandError)
    assert result.error == "Requested storage cell is occupied"
    robot.io_pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_demo_store_rejects_too_small_explicit_location() -> None:
    location = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0)
    robot = build_store_robot(build_grid(dimensions=Dimensions(width=1.0, length=1.0, height=1.0)))

    result = await robot.store(item_payload("ordinary-item", location))

    assert isinstance(result, RobotCommandError)
    assert result.error == "Item does not fit the requested storage cell"
    robot.io_pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_tray_return_keeps_the_tray_out_of_the_cell_inventory() -> None:
    """A returned tray must not show up as an inventory item at its home cell."""
    location = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0)
    grid = build_grid()
    robot = build_store_robot(grid, reserved_locations={("front_module", 0, 0)})
    robot.return_tray = AsyncMock()

    await robot.store(item_payload("0000000003", location), tray=True)

    robot.return_tray.assert_awaited_once()
    assert grid.get_cell(location).item is None
    assert grid.get_cell(location).status == CellStatus.AVAILABLE


@pytest.mark.asyncio
async def test_ordinary_store_rejects_tray_home() -> None:
    """A tray home looks available in the grid, but its tray is physically in the cell."""
    location = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0)
    robot = build_store_robot(build_grid(), reserved_locations={("front_module", 0, 0)})

    result = await robot.store(item_payload("ordinary-item", location))

    assert isinstance(result, RobotCommandError)
    assert result.error == "Requested storage cell is reserved"
    robot.io_pick.assert_not_awaited()
