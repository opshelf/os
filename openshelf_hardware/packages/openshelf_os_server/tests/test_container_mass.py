import importlib
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openshelf_os_server.core import robot_events
from openshelf_os_server.core.mass import apply_mass, apply_cycle_mass
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.robot_events import process_robot_event
from openshelf_os_server.core.validation import ValidationError
from openshelf_os_server.database.base import Base
from openshelf_os_server.database.models import Item


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


def test_mass_inputs_and_overflow():
    item = Item(upca='mass', name='mass', dimensions={}, weight=350)
    apply_mass(item, {'tare_weight': 100, 'net_weight': 250, 'full_net_weight': 500}, enrollment=True)
    assert item.net_weight == 250 and item.enrollment_weight_pending
    for values in ({'net_weight': -1}, {'tare_weight': float('nan')}, {'net_weight': 0, 'weight': None},
                   {'tare_weight': 1e308, 'net_weight': 1e308}):
        with pytest.raises(ValidationError):
            apply_mass(item, values)
    assert item.tare_weight == 100
    apply_mass(item, {'net_weight': 0})
    assert item.weight == 100 and item.net_weight == 0
    apply_mass(item, {'net_weight': None, 'full_net_weight': None})
    assert item.weight is None and item.full_net_weight is None


def test_below_tare_and_replay():
    item = Item(upca='mass', name='mass', dimensions={}, weight=350, tare_weight=100,
                enrollment_weight_pending=True)
    apply_cycle_mass(item, 345, stored=True, command_id='first')
    assert item.weight == 350 and not item.enrollment_weight_pending
    apply_cycle_mass(item, 300, stored=False, command_id='second')
    assert item.net_weight == 200
    assert not apply_cycle_mass(item, 345, stored=True, command_id='first')
    assert item.weight == 300
    apply_cycle_mass(item, 50, stored=False, command_id='third')
    assert item.weight == 50 and item.net_weight is None


@pytest.mark.anyio
async def test_durable_first_store_failed_retry_and_replay(cycle_session_factory):
    async with cycle_session_factory() as db:
        await StorageService(db).create_item('mass-cycle', 'Bottle', None,
            mass_values={'tare_weight': 100, 'net_weight': 250, 'full_net_weight': 500})
    base = {'upca': 'mass-cycle', 'location': {'side': 'left_module', 'shelf_idx': 0, 'cell_idx': 0}}
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


@pytest.mark.anyio
async def test_api_mass_null_omission_and_validation(client):
    response = await client.post('/api/storage/items', json={'upca': 'mass-api', 'name': 'Bottle', 'tare_weight': 100, 'net_weight': 250})
    assert response.status_code == 200, response.text
    assert response.json()['weight'] == 350
    response = await client.put('/api/storage/items/mass-api', json={'name': 'Renamed'})
    assert response.json()['net_weight'] == 250
    response = await client.put('/api/storage/items/mass-api', json={'tare_weight': None, 'net_weight': 1})
    assert response.status_code == 422
    response = await client.put('/api/storage/items/mass-api', json={'net_weight': None, 'full_net_weight': None})
    assert response.status_code == 200 and response.json()['weight'] is None
    response = await client.put('/api/storage/items/mass-api', json={'weight': None, 'net_weight': None})
    assert response.status_code == 422


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
