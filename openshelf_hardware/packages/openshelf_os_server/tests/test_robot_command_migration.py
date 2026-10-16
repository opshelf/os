from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError


def _migration() -> ModuleType:
	path = (
		Path(__file__).parents[1]
		/ "src/openshelf_os_server/alembic/versions/e2f3a4b5c6d7_generalize_robot_commands.py"
	)
	spec = importlib.util.spec_from_file_location("robot_command_migration", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _operations(migration: ModuleType, connection: sa.Connection) -> None:
	migration.op = Operations(MigrationContext.configure(connection))


def _legacy_commands(connection: sa.Connection) -> sa.Table:
	metadata = sa.MetaData()
	commands = sa.Table(
		"mock_robot_commands",
		metadata,
		sa.Column("command_id", sa.String(36), primary_key=True),
		sa.Column("request_id", sa.String(255), nullable=True, unique=True),
		sa.Column("payload", sa.JSON(), nullable=False),
		sa.Column("status", sa.String(20), nullable=False),
		sa.Column("result", sa.JSON(), nullable=True),
		sa.Column("correlation_id", sa.String(255), nullable=True),
		sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
		sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
	)
	metadata.create_all(connection)
	connection.execute(
		commands.insert().values(
			command_id="10000000-0000-0000-0000-000000000001",
			request_id="legacy-request",
			payload={"command": "store_item"},
			status="succeeded",
			created_at=datetime.now(timezone.utc),
		)
	)
	return commands


def _robot_commands(connection: sa.Connection) -> sa.Table:
	return sa.Table("robot_commands", sa.MetaData(), autoload_with=connection)


def test_upgrade_preserves_legacy_rows_and_scopes_request_id_uniqueness() -> None:
	engine = sa.create_engine("sqlite://")
	with engine.begin() as connection:
		_legacy_commands(connection)
		migration = _migration()
		_operations(migration, connection)
		migration.upgrade()

		inspector = sa.inspect(connection)
		assert not inspector.has_table("mock_robot_commands")
		assert inspector.has_table("robot_commands")
		assert inspector.get_unique_constraints("robot_commands") == [
			{"name": "uq_robot_commands_runtime_request_id", "column_names": ["runtime", "request_id"]}
		]

		commands = _robot_commands(connection)
		legacy = connection.execute(sa.select(commands)).mappings().one()
		assert legacy["runtime"] == "mock"
		assert legacy["visibility"] == "public"
		connection.execute(
			commands.insert().values(
				command_id="20000000-0000-0000-0000-000000000001",
				runtime="production",
				request_id="legacy-request",
				payload={"command": "store_item"},
				status="queued",
				visibility="public",
				created_at=datetime.now(timezone.utc),
			)
		)
		with pytest.raises(IntegrityError):
			connection.execute(
				commands.insert().values(
					command_id="30000000-0000-0000-0000-000000000001",
					runtime="mock",
					request_id="legacy-request",
					payload={"command": "store_item"},
					status="queued",
					visibility="public",
					created_at=datetime.now(timezone.utc),
				)
			)
