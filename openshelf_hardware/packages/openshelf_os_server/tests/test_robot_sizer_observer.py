from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from openshelf_os.robot.commands import StoreItemCommand
from openshelf_os_server.database.models import Item, ItemSizerMeasurement, RobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router

_TEST_BARCODE = "123456789012"


async def _clear_sizer_rows() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ItemSizerMeasurement))
        await session.execute(delete(Item).where(Item.barcode == _TEST_BARCODE))
        # Stores from earlier tests stay queued on the mock worker and would block this barcode.
        await session.execute(delete(RobotCommand).where(RobotCommand.runtime == "production"))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def reset_sizer_rows() -> AsyncGenerator[None, None]:
    await _clear_sizer_rows()
    yield
    await _clear_sizer_rows()


def _robot_enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(robot=SimpleNamespace(enabled=True))


def _worker() -> AsyncMock:
    worker = AsyncMock()
    worker.enqueue_command = AsyncMock()
    worker.needs_home = MagicMock(return_value=False)
    worker.needs_home_axes = MagicMock(return_value=[])
    worker.needs_home_reason = MagicMock(return_value=None)
    return worker


def _empty_item_db() -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.anyio
async def test_store_item_observer_capture_does_not_mutate_command_payload() -> None:
    worker = _worker()
    db = _empty_item_db()
    expected_dimensions = {"width": 1, "height": 2, "length": 3}
    payload = {
        "command": "store_item",
        "item": {
            "barcode": "123456789012",
            "name": "Test item",
            "dimensions": expected_dimensions,
        },
    }

    with (
        patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)),
        patch(
            "openshelf_os_server.routers.robot.capture_store_observer_measurement",
            new_callable=AsyncMock,
            return_value=None,
        ) as observer,
    ):
        await robot_router.robot_commands(
            command=StoreItemCommand(**payload),
            worker=worker,
            current_user=SimpleNamespace(id=1),
            db=db,
        )

    queued_command = worker.enqueue_command.await_args.args[0]
    assert queued_command.item == payload["item"]
    observer.assert_awaited_once()
    observer_args = observer.await_args
    assert observer_args is not None
    assert observer_args.kwargs["human_dimensions"] == expected_dimensions
    assert observer_args.kwargs["human_dimensions_source"] == "command_payload"


@pytest.mark.anyio
async def test_store_item_continues_when_observer_capture_raises(client: AsyncClient) -> None:
    worker = _worker()
    payload = {
        "command": "store_item",
        "item": {
            "barcode": "123456789012",
            "name": "Test item",
            "dimensions": {"width": 1, "height": 2, "length": 3},
        },
    }

    with (
        patch("openshelf_os_server.routers.robot.get_settings", return_value=_robot_enabled_settings()),
        patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker),
        patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)),
        patch(
            "openshelf_os_server.routers.robot.capture_store_observer_measurement",
            new_callable=AsyncMock,
            side_effect=Exception("measurement error"),
        ),
    ):
        response = await client.post("/api/robot/commands", json=payload)

    assert response.status_code == 200
    queued_command = worker.enqueue_command.await_args.args[0]
    assert queued_command.item == payload["item"]

