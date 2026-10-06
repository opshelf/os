"""Startup migration baseline: field databases are built by create_all, not by the chain.

Sentry #7743647918 logged `migration_failed` on every API start. The lifespan calls
`_run_migrations()` before `Base.metadata.create_all`, and no revision ever recorded a
version for the databases create_all had already brought to the current schema, so the
full history replayed on every start and aborted partway:

* on a database that already had the current schema, `a8b9c0d1e2f3` ran
  `CREATE INDEX ix_item_sizer_measurements_upca` for a column the rename revision had
  already renamed to `barcode`;
* on an empty database, `a4b5c6d7e8f9` batch-rebuilt `storage_bin_items`, whose `items`
  foreign key points at a table no revision creates.

Both must baseline with a stamp instead of replaying, while a database that is
genuinely behind the models still takes the replay path.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from openshelf_os_server import main
from openshelf_os_server.database.base import Base


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


def test_schema_state_reports_a_create_all_database_as_current(tmp_path) -> None:
    engine = _field_engine(tmp_path)
    state = main._schema_state(str(engine.url))

    assert state.current_revisions == []
    # Every model table and column is present, so there is nothing to migrate.
    assert state.missing == []
    assert state.tables_present == len(Base.metadata.tables)


def test_schema_state_reads_the_version_row_an_aborted_replay_left_behind(tmp_path) -> None:
    engine = _field_engine(tmp_path)
    head = _alembic_head(str(engine.url))

    # A replay that dies partway leaves the version rows sqlite committed around
    # its DDL behind: a mid-chain revision the database never actually migrated to.
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('a7b8c9d0e1f2')")

    state = main._schema_state(str(engine.url))

    assert state.current_revisions == ["a7b8c9d0e1f2"]
    assert state.current_revisions != [head]
    assert state.missing == []


def test_migrations_baseline_a_schema_current_database_without_replaying(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = _field_engine(tmp_path)
    url = str(engine.url)
    head = _alembic_head(url)

    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    assert recorder.named("migrations_baseline_stamped")[0]["reason"] == "schema_matches_models"
    assert _alembic_version(engine) == [head]
    # Baseline stamping is DDL-free for the schema itself: the model tables the
    # database already had must still hold their rows and definitions.
    assert set(sa.inspect(engine).get_table_names()) >= set(Base.metadata.tables)


def test_migrations_baseline_a_database_left_mid_chain_by_an_aborted_replay(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The state the alerting devices are in: the replay that died at
    # a8b9c0d1e2f3 left its committed version row mid-chain, while create_all had
    # already given the database the whole current schema.
    engine = _field_engine(tmp_path)
    url = str(engine.url)
    head = _alembic_head(url)

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('a7b8c9d0e1f2')")

    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    stamped = recorder.named("migrations_baseline_stamped")
    assert stamped[0]["reason"] == "schema_matches_models"
    assert stamped[0]["previous_revision"] == "a7b8c9d0e1f2"
    assert _alembic_version(engine) == [head]


def test_migrations_baseline_an_empty_database_without_replaying(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    head = _alembic_head(url)

    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    assert recorder.named("migrations_baseline_stamped")[0]["reason"] == "empty_database"
    assert recorder.names().count("running_database_migrations") == 0

    engine = sa.create_engine(url)
    assert _alembic_version(engine) == [head]
    # The lifespan creates the schema right after migrations; the stamp must not
    # get in its way.
    Base.metadata.create_all(engine)
    assert set(sa.inspect(engine).get_table_names()) >= set(Base.metadata.tables)


def test_migrations_still_upgrade_a_versioned_database(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = _field_engine(tmp_path)
    url = str(engine.url)
    head = _alembic_head(url)

    _run(monkeypatch, url)
    # Second start: the stamp exists now, so the incremental path runs and the
    # baseline stamp must not repeat.
    recorder.events.clear()
    _run(monkeypatch, url)

    assert recorder.failures() == [], f"migration_failed: {recorder.failures()}"
    assert recorder.names().count("migrations_baseline_stamped") == 0
    assert recorder.names().count("running_database_migrations") == 1
    assert _alembic_version(engine) == [head]


def test_migrations_replay_a_database_that_is_behind_the_models(
    recorder: _RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = _field_engine(tmp_path)

    # Roll one renamed column back: an old device database that still needs the
    # barcode rename revision.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_items_barcode")
        connection.exec_driver_sql("ALTER TABLE items RENAME COLUMN barcode TO upca")
        connection.exec_driver_sql("CREATE UNIQUE INDEX ix_items_upca ON items (upca)")

    url = str(engine.url)
    head = _alembic_head(url)
    assert main._schema_state(url).missing == ["items.barcode"]

    _run(monkeypatch, url)

    # A database that is behind must not be stamped onto a revision it never ran: it
    # keeps the replay path, which reports the drift it has to close. The replay
    # itself aborts here and leaves the partial version row its DDL committed.
    assert "migrations_baseline_stamped" not in recorder.names()
    assert _alembic_version(engine) != [head]
    replay = recorder.named("migrations_history_replay")
    assert replay != []
    assert "items.barcode" in replay[0]["missing"]
