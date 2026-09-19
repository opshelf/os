import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext


def _migration():
    path = (
        Path(__file__).parents[1]
        / "src/openshelf_os_server/alembic/versions/d8e9f0a1b2c3_widen_storage_bin_barcode.py"
    )
    spec = importlib.util.spec_from_file_location("widen_storage_bin_barcode_migration", path)
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


def _bin_schema(connection) -> None:
    """storage_bins as left by c0d1e2f3a4b5 + d1e2f3a4b5c6, with children."""
    metadata = sa.MetaData()
    items = sa.Table(
        "items",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
    )
    bins = sa.Table(
        "storage_bins",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("barcode", sa.String(12), nullable=False, unique=True),
        sa.Column("side", sa.String(20), nullable=False),
        sa.Column("shelf_idx", sa.Integer(), nullable=False),
        sa.Column("cell_idx", sa.Integer(), nullable=False),
        sa.Column("inner_dimensions", sa.JSON(), nullable=False),
        sa.Column("max_items", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("side", "shelf_idx", "cell_idx", name="uq_storage_bins_home_cell"),
    )
    sa.Index("ix_storage_bins_barcode", bins.c.barcode, unique=True)
    sa.Index("ix_storage_bins_status", bins.c.status)
    bin_items = sa.Table(
        "storage_bin_items",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "bin_id", sa.Integer(), sa.ForeignKey("storage_bins.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.UniqueConstraint("item_id", name="uq_storage_bin_items_item"),
    )
    sa.Index("ix_storage_bin_items_bin_id", bin_items.c.bin_id)
    metadata.create_all(connection)
    connection.execute(items.insert().values(id=1, name="Bottle"))
    connection.execute(
        bins.insert().values(
            id=1,
            barcode="TRAY-0001",
            side="left_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions="{}",
            max_items=4,
            status="at_home",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
    )
    connection.execute(bin_items.insert().values(id=1, bin_id=1, item_id=1))


def _barcode_length(connection) -> int:
    inspector = sa.inspect(connection)
    for column in inspector.get_columns("storage_bins"):
        if column["name"] == "barcode":
            return column["type"].length


def _assert_rows_intact(connection) -> None:
    # The rebuild drops and re-inserts storage_bins; with foreign keys off
    # the ON DELETE CASCADE on the children is not armed, so both parent
    # and child rows must survive.
    assert connection.execute(sa.text("SELECT id, barcode FROM storage_bins")).fetchall() == [
        (1, "TRAY-0001")
    ]
    assert connection.execute(sa.text("SELECT id, bin_id, item_id FROM storage_bin_items")).fetchall() == [
        (1, 1, 1)
    ]


def test_upgrade_widens_barcode_preserving_rows_and_downgrade_restores():
    migration = _migration()

    with _make_engine(foreign_keys=0).begin() as connection:
        _bin_schema(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        assert _barcode_length(connection) == 255

        # The rebuild must come back with its indexes and named constraint.
        inspector = sa.inspect(connection)
        inspector = sa.inspect(connection)
        index_names = {index["name"] for index in inspector.get_indexes("storage_bins")}
        assert index_names == {"ix_storage_bins_barcode", "ix_storage_bins_status"}
        barcode_index = next(
            index for index in inspector.get_indexes("storage_bins") if index["name"] == "ix_storage_bins_barcode"
        )
        assert barcode_index["unique"]
        assert any(
            constraint["name"] == "uq_storage_bins_home_cell"
            and constraint["column_names"] == ["side", "shelf_idx", "cell_idx"]
            for constraint in inspector.get_unique_constraints("storage_bins")
        )
        _assert_rows_intact(connection)

        migration.downgrade()

        assert _barcode_length(connection) == 12
        _assert_rows_intact(connection)


def test_upgrade_with_foreign_keys_on_raises_and_leaves_rows_untouched():
    migration = _migration()

    with _make_engine(foreign_keys=1).begin() as connection:
        _bin_schema(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="PRAGMA foreign_keys"):
            migration.upgrade()

        # The declared width is still the narrow one and nothing was
        # dropped on the way down.
        assert _barcode_length(connection) == 12
        _assert_rows_intact(connection)


def test_upgrade_is_idempotent_when_the_column_is_already_wide():
    migration = _migration()

    with _make_engine(foreign_keys=0).begin() as connection:
        _bin_schema(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()

        assert _barcode_length(connection) == 255
        _assert_rows_intact(connection)
