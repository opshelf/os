import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError


def test_compartment_migration_preserves_legacy_memberships_and_enforces_unique_slots():
    path = Path(__file__).parents[1] / "src/openshelf_os_server/alembic/versions/a4b5c6d7e8f9_add_tray_compartments.py"
    spec = importlib.util.spec_from_file_location("compartment_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE storage_bins (id INTEGER PRIMARY KEY, barcode TEXT)")
        connection.exec_driver_sql("CREATE TABLE storage_bin_items (id INTEGER PRIMARY KEY, bin_id INTEGER, item_id INTEGER UNIQUE)")
        connection.exec_driver_sql("CREATE TABLE storage_bin_workflows (id TEXT PRIMARY KEY, bin_id INTEGER)")
        connection.exec_driver_sql("INSERT INTO storage_bins VALUES (1, '900000000001')")
        connection.exec_driver_sql("INSERT INTO storage_bin_items VALUES (1, 1, 1), (2, 1, 2)")
        connection.exec_driver_sql("INSERT INTO storage_bin_workflows VALUES ('legacy', 1)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.exec_driver_sql("SELECT compartment FROM storage_bin_items ORDER BY id").all() == [(None,), (None,)]
        assert connection.exec_driver_sql("SELECT compartment FROM storage_bin_workflows").scalar() is None
        connection.exec_driver_sql("UPDATE storage_bin_items SET compartment = 1 WHERE id = 1")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE storage_bin_items SET compartment = 1 WHERE id = 2")
        connection.exec_driver_sql("UPDATE storage_bin_items SET compartment = 2 WHERE id = 2")
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT bin_id, item_id FROM storage_bin_items ORDER BY id").all() == [(1,1), (1,2)]
        migration.upgrade()
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM storage_bin_items").scalar() == 2
