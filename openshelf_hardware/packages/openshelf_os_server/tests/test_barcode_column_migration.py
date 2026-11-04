import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext


def _migration():
    path = (
        Path(__file__).parents[1]
        / "src/openshelf_os_server/alembic/versions/c7d8e9f0a1b2_rename_upca_columns_to_barcode.py"
    )
    spec = importlib.util.spec_from_file_location("barcode_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_engine(*, foreign_keys: int) -> sa.engine.Engine:
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _set_pragma_on_connect(dbapi_connection, _connection_record) -> None:
        # The raw DBAPI connection is outside any transaction here, so the
        # pragma actually takes effect before the migration runs.
        dbapi_connection.execute(f"PRAGMA foreign_keys={foreign_keys}")

    return engine


def _legacy_schema(connection) -> None:
    metadata = sa.MetaData()
    items = sa.Table(
        "items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("upca", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
    )
    sa.Index("ix_items_upca", items.c.upca, unique=True)
    locations = sa.Table(
        "item_locations",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("side", sa.String(20), nullable=False, server_default="front_module"),
    )
    measurements = sa.Table(
        "item_sizer_measurements",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("upca", sa.String(255), nullable=True),
    )
    sa.Index("ix_item_sizer_measurements_upca", measurements.c.upca)
    metadata.create_all(connection)
    connection.execute(items.insert().values(id=1, upca="CR-00006174/A", name="Bottle"))
    connection.execute(locations.insert().values(id=1, item_id=1, side="front_module"))
    connection.execute(measurements.insert().values(id=1, item_id=1, upca="CR-00006174/A"))


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_upgrade_renames_columns_and_indexes_preserving_values(foreign_keys):
    migration = _migration()

    with _make_engine(foreign_keys=foreign_keys).begin() as connection:
        _legacy_schema(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {column["name"] for column in inspector.get_columns("items")} == {"id", "barcode", "name"}
        assert {column["name"] for column in inspector.get_columns("item_sizer_measurements")} == {
            "id",
            "item_id",
            "barcode",
        }
        assert "ix_items_barcode" in {index["name"] for index in inspector.get_indexes("items")}
        assert "ix_item_sizer_measurements_barcode" in {
            index["name"] for index in inspector.get_indexes("item_sizer_measurements")
        }

        # Values are strings either way, so the rename must not touch them.
        assert connection.execute(sa.text("SELECT barcode FROM items WHERE id = 1")).scalar() == "CR-00006174/A"
        assert (
            connection.execute(sa.text("SELECT barcode FROM item_sizer_measurements WHERE id = 1")).scalar()
            == "CR-00006174/A"
        )

        # A rename implemented as a table rebuild would drop and re-insert the
        # parent rows; with foreign keys enforced that fires ON DELETE CASCADE
        # on the occupancy rows and ON DELETE SET NULL on the measurement.
        location_rows = connection.execute(
            sa.text("SELECT id, item_id, side FROM item_locations")
        )
        assert location_rows.fetchall() == [(1, 1, "front_module")]
        measurement_rows = connection.execute(sa.text("SELECT id, item_id FROM item_sizer_measurements"))
        assert measurement_rows.fetchall() == [(1, 1)]
        assert connection.execute(sa.text("PRAGMA foreign_key_check")).fetchall() == []
