"""The side-value rename must survive the startup baseline stamp.

`_schema_state` compares table and column names only, so the data-only revision
f9a0b1c2d3e4 (item_locations/storage_bins `side` values: left/right to
front/back) is invisible to it. Field databases that create_all had already
brought to the current schema were stamped at the previous head by earlier
starts; on this branch's first start the stamp branch stamps f9a0b1c2d3e4
without running its UPDATEs, and the old side values survive under code that
no longer matches them (occupied cells reported available, bin and location
lookups missing, both spellings coexisting). The startup path therefore
re-runs the idempotent rename after both of its own paths, and the revision
calls the same helper on the normal upgrade route.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from openshelf_os_server import main
from openshelf_os_server.database.base import Base

# The data-only revision under test and the head it extended; later revisions may follow it.
PREVIOUS_HEAD = "d8e9f0a1b2c3"
SIDE_VALUE_REVISION = "f9a0b1c2d3e4"


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    info = debug
    warning = debug
    error = debug

    def names(self) -> list[str]:
        return [event for event, _ in self.events]

    def failures(self) -> list[dict[str, Any]]:
        return [kwargs for event, kwargs in self.events if event == "migration_failed"]

    def named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for event, kwargs in self.events if event == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    recording = _RecordingLogger()
    monkeypatch.setattr(main, "logger", recording)
    return recording


def _ancestry(database_url: str, head: str) -> set[str]:
    config = main._alembic_config(database_url)
    assert config is not None
    return {
        script.revision
        for script in ScriptDirectory.from_config(config).walk_revisions("base", head)
    }


def _alembic_head(database_url: str) -> str:
    config = main._alembic_config(database_url)
    assert config is not None
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    return heads[0]


def _field_engine(tmp_path, name: str = "field.db") -> sa.engine.Engine:
    # The lifespan creates the schema from the models; that is the baseline every
    # deployed database carries.
    engine = sa.create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine


def _alembic_version(engine: sa.engine.Engine) -> list[str]:
    if "alembic_version" not in sa.inspect(engine).get_table_names():
        return []
    with engine.connect() as connection:
        return [row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")]


def _run(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setattr(main, "get_database_url", lambda: url)
    main._run_migrations()


def _stamp(engine: sa.engine.Engine, revision: str) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{revision}')")


def _insert_old_side_values(engine: sa.engine.Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO items (id, barcode, name, dimensions) VALUES (1, 'CR-00006174/A', 'Bottle', '{}')"
        )
        connection.exec_driver_sql(
            "INSERT INTO item_locations (id, item_id, side, shelf_idx, cell_idx) "
            "VALUES (1, 1, 'left_module', 0, 0)"
        )
        connection.exec_driver_sql(
            "INSERT INTO item_locations (id, item_id, side, shelf_idx, cell_idx) "
            "VALUES (2, 1, 'right_module', 0, 1)"
        )
        for bin_id, barcode, side, cell_idx in ((1, "BIN-001", "left_module", 2), (2, "BIN-002", "right_module", 3)):
            connection.exec_driver_sql(
                "INSERT INTO storage_bins (id, barcode, side, shelf_idx, cell_idx, inner_dimensions, "
                "max_items, status, active, created_at, updated_at) "
                f"VALUES ({bin_id}, '{barcode}', '{side}', 0, {cell_idx}, '{{}}', 4, 'at_home', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )


def _sides(target: sa.engine.Engine | sa.engine.Connection, table: str) -> list[str]:
    if isinstance(target, sa.engine.Engine):
        with target.connect() as connection:
            return _sides(connection, table)
    return [row[0] for row in target.exec_driver_sql(f"SELECT side FROM {table} ORDER BY id")]


def test_migrations_baseline_stamp_renames_module_side_values(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The state field databases are in at this branch's first start:
    # create_all built the whole current schema, earlier starts stamped the
    # previous head, and the rows still carry the old side values.
    engine = _field_engine(tmp_path)
    url = str(engine.url)
    head = _alembic_head(url)
    assert SIDE_VALUE_REVISION in _ancestry(url, head)

    _stamp(engine, PREVIOUS_HEAD)
    _insert_old_side_values(engine)

    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    stamped = recorder.named("migrations_baseline_stamped")
    assert stamped[0]["reason"] == "schema_matches_models"
    assert stamped[0]["previous_revision"] == PREVIOUS_HEAD
    assert _alembic_version(engine) == [head]
    # The stamp alone would have skipped the data-only revision; the
    # post-stamp re-run must have renamed every row.
    assert _sides(engine, "item_locations") == ["front_module", "back_module"]
    assert _sides(engine, "storage_bins") == ["front_module", "back_module"]


def test_migrations_replay_renames_module_side_values(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = _field_engine(tmp_path)
    url = str(engine.url)
    head = _alembic_head(url)
    assert SIDE_VALUE_REVISION in _ancestry(url, head)

    _stamp(engine, PREVIOUS_HEAD)
    _insert_old_side_values(engine)

    # Force the replay path: roll one renamed column back so the schema no
    # longer matches the models. The replay from the previous head reaches the
    # data-only revision, and the startup path re-runs the rename after it.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_items_barcode")
        connection.exec_driver_sql("ALTER TABLE items RENAME COLUMN barcode TO upca")
        connection.exec_driver_sql("CREATE UNIQUE INDEX ix_items_upca ON items (upca)")

    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    assert "migrations_baseline_stamped" not in recorder.names()
    assert _alembic_version(engine) == [head]
    assert _sides(engine, "item_locations") == ["front_module", "back_module"]
    assert _sides(engine, "storage_bins") == ["front_module", "back_module"]
