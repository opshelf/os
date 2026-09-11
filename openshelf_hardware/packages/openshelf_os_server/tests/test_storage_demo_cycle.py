import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from openshelf_os.storage import StorageModuleCellLocation, StorageModuleSide
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
@pytest.mark.parametrize("reservation", ("configured", "active", "member", "outside"))
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
                status="outside" if reservation == "outside" else "at_home",
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
