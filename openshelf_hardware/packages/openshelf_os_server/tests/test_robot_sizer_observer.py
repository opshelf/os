from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os.iobox_sizer.models import IOBoxSizerDimensions, IOBoxSizerMeasurement
from openshelf_os.robot.commands import StoreItemCommand
from openshelf_os_server.core.iobox_sizer import record_store_sizer_measurement
from openshelf_os_server.database.models import Item, ItemSizerMeasurement
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router


def _robot_enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(robot=SimpleNamespace(enabled=True))


def _worker() -> AsyncMock:
    worker = AsyncMock()
    worker.enqueue_command = AsyncMock()
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
            "upca": "123456789012",
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
        "item": {"upca": "123456789012", "name": "Test item"},
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


@pytest.mark.anyio
async def test_record_store_sizer_measurement_persists_manual_and_auto_dimensions() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ItemSizerMeasurement))
        await session.execute(delete(Item).where(Item.upca == "123456789012"))
        item = Item(
            upca="123456789012",
            name="Test item",
            dimensions={"width": 10, "height": 20, "length": 30},
        )
        session.add(item)
        await session.commit()

    timestamp = datetime.now(timezone.utc)
    measurement = IOBoxSizerMeasurement(
        detected=True,
        mode="difference",
        width_px=100,
        height_px=200,
        frame_width=640,
        frame_height=480,
        dimensions_mm=IOBoxSizerDimensions(width=11, height=21, length=31),
        timestamp=timestamp,
        duration_ms=123,
    )

    await record_store_sizer_measurement(
        upca="123456789012",
        item_name="Test item",
        user_id=1,
        request_correlation_id="correlation-id",
        command_payload={"command": "store_item"},
        human_dimensions={"width": 10, "height": 20, "length": 30},
        human_dimensions_source="db",
        measurement=measurement,
        error=None,
        should_raise=True,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ItemSizerMeasurement))
        row = result.scalar_one()

    assert row.item_id is not None
    assert row.upca == "123456789012"
    assert row.item_name == "Test item"
    assert row.user_id == 1
    assert row.request_correlation_id == "correlation-id"
    assert row.manual_dimensions == {"width": 10, "height": 20, "length": 30}
    assert row.manual_dimensions_source == "db"
    assert row.auto_dimensions == {"width": 11.0, "height": 21.0, "length": 31.0}
    assert row.raw_measurement is not None
    assert row.raw_measurement["duration_ms"] == 123
    assert row.command_payload == {"command": "store_item"}
    assert row.detected is True
    assert row.error_type is None
