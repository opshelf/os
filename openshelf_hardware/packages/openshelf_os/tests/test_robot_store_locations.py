from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.robot.core import Robot
from openshelf_os.robot.status import RobotCommandError, RobotCommandSuccessful
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
                shelf_width=400.0,
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


def build_demo_store_robot(
    demo_cell_map: dict[str, StorageModuleCellLocation] | None,
    reserved_locations: set[tuple[str, int, int]],
) -> tuple[Robot, StoreGrid]:
    grid = StoreGrid()
    return build_store_robot(grid, demo_cell_map, reserved_locations), grid


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
async def test_demo_store_allows_trusted_explicit_home() -> None:
    home = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=1, cell_idx=2)
    robot, grid = build_demo_store_robot(
        {},
        {(home.side.value, home.shelf_idx, home.cell_idx)},
    )

    result = await robot.store(item_payload("bin-123", home))

    assert isinstance(result, RobotCommandSuccessful)
    assert result.location == home
    assert grid.stored_locations == [home]


@pytest.mark.asyncio
async def test_demo_store_uses_explicit_home_over_conflicting_demo_mapping() -> None:
    home = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=1, cell_idx=2)
    mapped_location = StorageModuleCellLocation(side=StorageModuleSide.BACK, shelf_idx=3, cell_idx=4)
    robot, grid = build_demo_store_robot(
        {"bin-123": mapped_location},
        {(home.side.value, home.shelf_idx, home.cell_idx)},
    )

    result = await robot.store(item_payload("bin-123", home))

    assert isinstance(result, RobotCommandSuccessful)
    assert result.location == home
    assert grid.stored_locations == [home]
    robot.io_pick.assert_awaited_once()


@pytest.mark.asyncio
async def test_demo_store_allows_explicit_home_for_unmapped_bin_barcode() -> None:
    home = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=1, cell_idx=2)
    robot, grid = build_demo_store_robot(
        {"ordinary-item": StorageModuleCellLocation(side=StorageModuleSide.BACK, shelf_idx=0, cell_idx=0)},
        {(home.side.value, home.shelf_idx, home.cell_idx)},
    )

    result = await robot.store(item_payload("bin-456", home))

    assert isinstance(result, RobotCommandSuccessful)
    assert result.location == home
    assert grid.stored_locations == [home]


@pytest.mark.asyncio
async def test_demo_store_uses_mapping_over_ordinary_explicit_location() -> None:
    mapped_location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT, shelf_idx=1, cell_idx=2
    )
    ordinary_location = StorageModuleCellLocation(
        side=StorageModuleSide.BACK, shelf_idx=3, cell_idx=4
    )
    robot, grid = build_demo_store_robot(
        {"ordinary-item": mapped_location},
        set(),
    )

    result = await robot.store(item_payload("ordinary-item", ordinary_location))

    assert isinstance(result, RobotCommandSuccessful)
    assert result.location == mapped_location
    assert grid.stored_locations == [mapped_location]


@pytest.mark.asyncio
async def test_demo_store_rejects_unmapped_barcode_with_ordinary_explicit_location() -> None:
    ordinary_location = StorageModuleCellLocation(
        side=StorageModuleSide.BACK, shelf_idx=3, cell_idx=4
    )
    robot, grid = build_demo_store_robot({}, set())

    result = await robot.store(item_payload("ordinary-item", ordinary_location))

    assert isinstance(result, RobotCommandError)
    assert result.error == "barcode ordinary-item not in demo mapping"
    assert grid.stored_locations == []
    robot.io_pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_demo_store_rejects_unknown_explicit_location() -> None:
    explicit_location = StorageModuleCellLocation(
        side=StorageModuleSide.BACK, shelf_idx=3, cell_idx=4
    )
    grid = build_grid()
    robot = build_store_robot(grid)

    result = await robot.store(item_payload("ordinary-item", explicit_location))

    assert isinstance(result, RobotCommandError)
    assert result.error == "Requested storage cell does not exist"
    robot.io_pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_demo_store_stores_to_available_explicit_location() -> None:
    location = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0)
    grid = build_grid()
    robot = build_store_robot(grid)

    result = await robot.store(item_payload("ordinary-item", location))

    assert isinstance(result, RobotCommandSuccessful)
    assert result.location == location
    assert grid.get_cell(location).status == CellStatus.OCCUPIED
    robot.io_pick.assert_awaited_once()


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
async def test_non_demo_store_rejects_malformed_explicit_location() -> None:
    robot = build_store_robot(build_grid())
    payload = item_payload("ordinary-item")
    payload["location"] = {"side": "middle_module", "shelf_idx": 0, "cell_idx": 0}

    result = await robot.store(payload)

    assert isinstance(result, RobotCommandError)
    assert result.error == "Invalid storage location provided"
    robot.io_pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_store_rejects_mapped_reserved_location_without_explicit_location() -> None:
    home = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=1, cell_idx=2)
    robot, grid = build_demo_store_robot(
        {"ordinary-item": home},
        {(home.side.value, home.shelf_idx, home.cell_idx)},
    )

    result = await robot.store(item_payload("ordinary-item"))

    assert isinstance(result, RobotCommandError)
    assert result.error == "Demo storage location is reserved"
    assert grid.stored_locations == []
    robot.io_pick.assert_not_awaited()
