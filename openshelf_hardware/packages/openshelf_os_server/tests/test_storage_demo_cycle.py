import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import FridgeSafetyError
from openshelf_os.storage import StorageModuleCellLocation, StorageModuleSide
from openshelf_os.tool import Tool
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import StorageBin, StorageBinItem
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import demo


def _config(*entries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(storage=SimpleNamespace(bins=list(entries)))


def _bin_config(location: StorageModuleCellLocation) -> SimpleNamespace:
    return _config(
        SimpleNamespace(
            barcode="900000000099",
            home_location=SimpleNamespace(
                side=location.side.value,
                shelf_idx=location.shelf_idx,
                cell_idx=location.cell_idx,
            ),
            inner_dimensions=SimpleNamespace(
                model_dump=lambda mode: {"width": 30, "height": 30, "length": 30}
            ),
            max_items=2,
        )
    )


def _demo_location(cell_idx: int = 0) -> StorageModuleCellLocation:
    return StorageModuleCellLocation(
        side=StorageModuleSide.LEFT,
        shelf_idx=0,
        cell_idx=cell_idx,
    )


def _prepare_demo_start(
    monkeypatch: pytest.MonkeyPatch,
    location: StorageModuleCellLocation,
    *,
    runtime_reserved: set[tuple[str, int, int]] | None = None,
) -> AsyncMock:
    robot = SimpleNamespace(
        _demo_cell_map={"demo-item": location},
        _reserved_cell_locations=runtime_reserved or set(),
        _tool=MagicMock(),
    )
    worker = SimpleNamespace(_robot=robot)
    clear_motors = AsyncMock()

    monkeypatch.setattr(demo, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(demo, "get_robot_worker_or_503", AsyncMock(return_value=worker))
    monkeypatch.setattr(demo, "_ensure_ready", lambda _worker: None)
    monkeypatch.setattr(
        demo,
        "_build_cell_list",
        lambda _robot: [(location, MagicMock(), None)],
    )
    monkeypatch.setattr(demo, "_clear_motors_for_move", clear_motors)
    return clear_motors


@pytest.fixture(autouse=True)
def reset_demo_cycle_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo, "_demo_task", None)
    monkeypatch.setattr(demo, "_stop_event", asyncio.Event())
    monkeypatch.setattr(demo, "_is_running", False)
    monkeypatch.setattr(demo, "_current_cell_index", None)
    monkeypatch.setattr(demo, "_total_cells", None)


@pytest.mark.anyio
@pytest.mark.parametrize("reservation", ("configured", "active", "member", "quarantined"))
async def test_demo_start_rejects_every_reserved_bin_home(
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
) -> None:
    location = _demo_location()
    clear_motors = _prepare_demo_start(monkeypatch, location)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(location) if reservation == "configured" else _config())

    if reservation != "configured":
        async with AsyncSessionLocal() as session:
            bin_row = StorageBin(
                barcode=f"9000000000{len(reservation):02d}",
                side=location.side.value,
                shelf_idx=location.shelf_idx,
                cell_idx=location.cell_idx,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=2,
                status="quarantined" if reservation == "quarantined" else "at_home",
                active=reservation == "active",
            )
            session.add(bin_row)
            if reservation == "member":
                await session.flush()
                item = await StorageService(session).create_item(
                    "123456789099",
                    "Bin member",
                    {"width": 1, "height": 1, "length": 1},
                )
                session.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
            await session.commit()

    with pytest.raises(HTTPException) as error:
        await demo.start_demo_cycle()

    assert error.value.status_code == 409
    assert error.value.detail == "Demo cell map includes a reserved storage bin home"
    clear_motors.assert_not_awaited()


@pytest.mark.anyio
async def test_demo_start_defensively_checks_robot_runtime_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _demo_location()
    clear_motors = _prepare_demo_start(
        monkeypatch,
        location,
        runtime_reserved={(location.side.value, location.shelf_idx, location.cell_idx)},
    )
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _config())

    with pytest.raises(HTTPException) as error:
        await demo.start_demo_cycle()

    assert error.value.status_code == 409
    clear_motors.assert_not_awaited()


@pytest.mark.anyio
async def test_demo_start_leaves_nonreserved_mapping_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _demo_location()
    clear_motors = _prepare_demo_start(monkeypatch, location)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _config())
    monkeypatch.setattr(demo, "_run_demo_cycle", AsyncMock())

    await demo.start_demo_cycle()
    await asyncio.sleep(0)

    clear_motors.assert_awaited_once()
    assert demo._is_running is True
    await demo.shutdown_demo_cycle()


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


@pytest.mark.anyio
async def test_demo_start_serializes_reservation_check_with_bin_workflow_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _demo_location()
    clear_motors = _prepare_demo_start(monkeypatch, location)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _config())
    monkeypatch.setattr(bin_storage, "_command_admission_lock", asyncio.Lock())
    monkeypatch.setattr(demo, "_run_demo_cycle", AsyncMock())

    reservation_read_started = asyncio.Event()
    release_reservation_read = asyncio.Event()
    original_reserved_bin_locations = bin_storage.reserved_bin_locations

    async def delayed_reserved_bin_locations(session):
        reservation_read_started.set()
        await release_reservation_read.wait()
        return await original_reserved_bin_locations(session)

    monkeypatch.setattr(demo, "reserved_bin_locations", delayed_reserved_bin_locations)
    demo_task = asyncio.create_task(demo.start_demo_cycle())
    await reservation_read_started.wait()

    async def competing_bin_admission() -> None:
        async with bin_storage.bin_workflow_command_admission_guard():
            return None

    bin_task = asyncio.create_task(competing_bin_admission())
    await asyncio.sleep(0)
    assert bin_task.done() is False

    release_reservation_read.set()
    await demo_task
    await bin_task

    assert clear_motors.await_count == 1
    await demo.shutdown_demo_cycle()
