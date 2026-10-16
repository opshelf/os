"""The planned_dimensions column reaches deployed databases that are at the previous head."""

import sqlalchemy as sa

from openshelf_os_server import main
from openshelf_os_server.database.base import Base

PREVIOUS_HEAD = "b6c7d8e9f0a1"
CURRENT_HEAD = "4c1d2e3f5a6b"
LOGIN_BARCODE_COLUMN = "login_barcode"
LOGIN_BARCODE_INDEX = "ix_users_login_barcode"


def _columns(engine: sa.engine.Engine) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns("import_job_units")}


def _versions(engine: sa.engine.Engine) -> list[str]:
    with engine.connect() as connection:
        return [row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")]


def _indexes(engine: sa.engine.Engine) -> dict[str, dict]:
    return {index["name"]: index for index in sa.inspect(engine).get_indexes("users")}


def test_existing_database_at_previous_head_gains_the_column_in_place(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'field.db'}"
    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX {LOGIN_BARCODE_INDEX}")
        connection.exec_driver_sql(f"ALTER TABLE users DROP COLUMN {LOGIN_BARCODE_COLUMN}")
        connection.exec_driver_sql("ALTER TABLE import_job_units DROP COLUMN planned_dimensions")
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{PREVIOUS_HEAD}')")
        connection.exec_driver_sql(
            "INSERT INTO import_jobs (id, state, created_at, updated_at) VALUES ('j', 'active', '2026-01-01', '2026-01-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO import_job_units (job_id, position, barcode, name, small, unit_metadata, status, attempts, updated_at) "
            "VALUES ('j', 0, 'B-1', 'Kept', 0, '{}', 'pending', 0, '2026-01-01')"
        )
    assert "planned_dimensions" not in _columns(engine)

    monkeypatch.setattr(main, "get_database_url", lambda: url)
    main._run_migrations()

    assert "planned_dimensions" in _columns(engine)
    assert LOGIN_BARCODE_COLUMN in {column["name"] for column in sa.inspect(engine).get_columns("users")}
    assert _indexes(engine)[LOGIN_BARCODE_INDEX]["unique"] == 1
    assert _versions(engine) == [CURRENT_HEAD]
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT name, planned_dimensions FROM import_job_units").one()
    assert tuple(row) == ("Kept", None)

    main._run_migrations()  # idempotent
    assert _versions(engine) == [CURRENT_HEAD]
