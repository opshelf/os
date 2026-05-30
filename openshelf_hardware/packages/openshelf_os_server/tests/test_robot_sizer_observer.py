from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os.iobox_sizer.models import IOBoxSizerDimensions, IOBoxSizerMeasurement
from openshelf_os_server.core.iobox_sizer import record_store_sizer_measurement
from openshelf_os_server.database.models import Item, ItemSizerMeasurement
from openshelf_os_server.database.session import AsyncSessionLocal


def _settings() -> SimpleNamespace:
    ...


def _worker() -> AsyncMock:
    ...


@pytest.mark.anyio
async def test_store_item_observer_capture_does_not_mutate_command_payload(client: AsyncClient) -> None:
    worker = _worker()
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
        patch("openshelf_os_server.core.iobox_sizer.get_iobox_sizer_config", return_value=_settings()),
        patch("openshelf_os_server.core.iobox_sizer.CameraManager", autospec=True) as mock_cmr,
        patch(
            "openshelf_os_server.core.iobox_sizer.capture_store_observer_measurement",
            return_value=None,
        ) as observer,
    ):
        from openshelf_os_server.api.routes.robot_commands import enqueue_store

        await enqueue_store(
            worker=worker,
            command=payload,
            user_id=1,
            request_correlation_id="test-correlation-id",
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
        patch("openshelf_os_server.core.iobox_sizer.get_iobox_sizer_config", return_value=_settings()),
        patch("openshelf_os_server.core.iobox_sizer.CameraManager", autospec=True) as mock_cmr,
        patch(
            "openshelf_os_server.core.iobox_sizer.capture_store_observer_measurement",
            side_effect=Exception("measurement error"),
        ),
    ):
        from openshelf_os_server.api.routes.robot_commands import enqueue_store

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
