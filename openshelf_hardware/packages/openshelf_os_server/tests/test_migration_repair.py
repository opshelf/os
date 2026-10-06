"""Repair for devices left mid-chain by the aborted tray-compartment replay.

Devices provisioned before the baseline stamp ran `upgrade head` on an empty
database. The chain built its own tables up to f3a4b5c6d7e8, a4b5c6d7e8f9 added its
three columns, and then its batch rebuild reflected `storage_bin_items`' foreign key
to `items` - a table no revision creates - and aborted. The lifespan created `items`
from the models right after, so every later start replayed a4b5c6d7e8f9 into columns
that were already there and died on `duplicate column name`, leaving
`item_sizer_measurements` on `upca` and storage_bin_items without its compartment
constraint.

The reconstruction below mirrors that sequence exactly, then asserts the replay
completes: the device is repaired by the normal migration path, not by hand.
"""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from openshelf_os_server import main
from openshelf_os_server.database.base import Base


def _head(database_url: str) -> str:
    config = main._alembic_config(database_url)
    assert config is not None
    heads = sorted(ScriptDirectory.from_config(config).get_heads())
    assert len(heads) == 1
    return heads[0]


def _columns(engine: sa.engine.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def _unique_constraints(engine: sa.engine.Engine, table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(engine).get_unique_constraints(table)
    }


def _alembic_version(engine: sa.engine.Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        ]


def _reconstruct_broken_device(url: str) -> sa.engine.Engine:
    config = main._alembic_config(url)
    assert config is not None

    # The chain-native schema up to the last revision that committed on the device.
    command.upgrade(config, "f3a4b5c6d7e8")

    # The replay that aborted inside a4b5c6d7e8f9: the three columns commit around its
    # DDL, the batch rebuild then cannot reflect the foreign key to the model-only
    # `items` table, and the version row stays where it was.
    with pytest.raises(Exception):
        command.upgrade(config, "head")

    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)

    assert _alembic_version(engine) == ["f3a4b5c6d7e8"]
    assert {"compartment_layout"} <= _columns(engine, "storage_bins")
    assert {"compartment"} <= _columns(engine, "storage_bin_items")
    assert "uq_storage_bin_items_compartment" not in _unique_constraints(
        engine, "storage_bin_items"
    )
    assert "upca" in _columns(engine, "item_sizer_measurements")
    return engine


def test_replay_repairs_a_database_left_mid_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{tmp_path / 'broken.db'}"
    engine = _reconstruct_broken_device(url)

    monkeypatch.setattr(main, "get_database_url", lambda: url)
    main._run_migrations()

    captured = capsys.readouterr()
    assert "duplicate column name" not in captured.err

    assert _alembic_version(engine) == [_head(url)]
    assert main._schema_state(url).missing == []
    assert "barcode" in _columns(engine, "item_sizer_measurements")
    assert "upca" not in _columns(engine, "item_sizer_measurements")
    assert "uq_storage_bin_items_compartment" in _unique_constraints(
        engine, "storage_bin_items"
    )


def test_replay_is_a_no_op_after_the_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    url = f"sqlite:///{tmp_path / 'repaired.db'}"
    _reconstruct_broken_device(url)

    monkeypatch.setattr(main, "get_database_url", lambda: url)
    main._run_migrations()
    main._run_migrations()

    assert _alembic_version(sa.create_engine(url)) == [_head(url)]
