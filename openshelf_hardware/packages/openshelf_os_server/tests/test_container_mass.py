import importlib
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openshelf_os_server.core import robot_events
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.robot_events import process_robot_event
from openshelf_os_server.database.base import Base


@pytest.fixture
async def cycle_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mass-cycle.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        # Event mutations and telemetry must share this test's isolated database.
        monkeypatch.setattr(robot_events, 'AsyncSessionLocal', session_factory)
        monkeypatch.setattr(robot_events.robot_core, 'robot_worker', None)
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_durable_first_store_failed_retry_and_replay(cycle_session_factory):
    async with cycle_session_factory() as db:
        await StorageService(db).create_item('mass-cycle', 'Bottle', None,
            mass_values={'tare_weight': 100, 'net_weight': 250, 'full_net_weight': 500})
    base = {'barcode': 'mass-cycle', 'location': {'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0}}
    first = {**base, 'command': 'store_item', 'command_id': 'mass-first', 'message': 'success', 'weight': 345}
    await process_robot_event({**first, 'message': 'failure'})
    async with cycle_session_factory() as db:
        item = await StorageService(db).get_item('mass-cycle')
        assert item.enrollment_weight_pending
    await process_robot_event(first)
    async with cycle_session_factory() as db:
        item = await StorageService(db).get_item('mass-cycle')
        assert item.weight == 350 and item.enrollment_storage_completed
    await process_robot_event({**base, 'command': 'retrieve_item', 'command_id': 'mass-second', 'message': 'success', 'weight': 300})
    await process_robot_event(first)
    async with cycle_session_factory() as db:
        service = StorageService(db)
        item = await service.get_item('mass-cycle')
        assert item.net_weight == 200
        assert not await service.get_item_locations('mass-cycle')
        await service.update_item('mass-cycle', metadata={'name': 'edit'})
        assert not item.enrollment_weight_pending


def test_migration_fresh_existing_and_repeated():
    migration = importlib.import_module('openshelf_os_server.alembic.versions.f3a4b5c6d7e8_add_container_mass')
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        with patch.object(migration, 'op', Operations(MigrationContext.configure(connection))):
            migration.upgrade()  # Fresh startup creates the model tables later.
            connection.execute(text('CREATE TABLE items (id INTEGER PRIMARY KEY, weight FLOAT)'))
            connection.execute(text('INSERT INTO items VALUES (1, 350)'))
            migration.upgrade()
            migration.upgrade()
            row = connection.execute(text('SELECT * FROM items')).mappings().one()
            assert row['weight'] == 350 and row['tare_weight'] is None
            assert row['enrollment_weight_pending'] == 0 and row['enrollment_storage_completed'] == 1
