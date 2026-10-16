import importlib.util
from pathlib import Path

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


def _bin_schema(connection, *, barcode_length: int = 12) -> None:
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
        sa.Column("barcode", sa.String(barcode_length), nullable=False, unique=True),
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
            side="front_module",
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


def _as_dialect(connection, dialect_name: str) -> None:
    """Pretend the (real, in-memory) SQLite connection is another dialect.

    The migration only reads the dialect name to pick its branch; the
    reflection and row data stay real SQLite. The mutation is on this
    test's own engine, so it cannot leak into other tests.
    """
    connection.dialect.name = dialect_name


def _record_alter_columns(migration, connection) -> list:
    """Point the migration at the connection and record its DDL requests.

    alter_column is stubbed so the DDL the migration would request can be
    asserted on instead of executed: an empty record is proof that no DDL
    ran at all (no rebuild, no row risk).
    """
    migration.op = Operations(MigrationContext.configure(connection))
    calls: list = []

    def fake_alter_column(table, column, **kwargs):
        calls.append((table, column, kwargs))

    migration.op.alter_column = fake_alter_column
    return calls


def _barcode_length(connection) -> int:
    inspector = sa.inspect(connection)
    for column in inspector.get_columns("storage_bins"):
        if column["name"] == "barcode":
            return column["type"].length


def _assert_rows_intact(connection) -> None:
    assert connection.execute(sa.text("SELECT id, barcode FROM storage_bins")).fetchall() == [
        (1, "TRAY-0001")
    ]
    assert connection.execute(sa.text("SELECT id, bin_id, item_id FROM storage_bin_items")).fetchall() == [
        (1, 1, 1)
    ]


def test_upgrade_on_sqlite_leaves_width_and_preserves_rows_indexes_and_constraint():
    migration = _migration()

    with sa.create_engine("sqlite://").begin() as connection:
        _bin_schema(connection)
        _as_dialect(connection, "sqlite")
        calls = _record_alter_columns(migration, connection)
        migration.upgrade()

        # SQLite: the declared width is left alone (it is unenforced
        # metadata), no rebuild runs, so rows, children, indexes, and the
        # named constraint all come through untouched.
        assert calls == []
        assert _barcode_length(connection) == 12
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
