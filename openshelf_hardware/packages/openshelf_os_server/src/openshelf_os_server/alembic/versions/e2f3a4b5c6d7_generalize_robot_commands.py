"""generalize durable robot commands to all operating modes

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""

from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


REQUEST_ID_UNIQUE = "uq_robot_commands_runtime_request_id"


def _has_composite_request_id_uniqueness(inspector: sa.Inspector) -> bool:
	return any(
		constraint.get("column_names") == ["runtime", "request_id"]
		for constraint in inspector.get_unique_constraints("robot_commands")
	) or any(
		index.get("unique") and index.get("column_names") == ["runtime", "request_id"]
		for index in inspector.get_indexes("robot_commands")
	)


def _table_without_unique_constraints(bind: sa.Connection) -> sa.Table:
	"""Reflect columns only so SQLite batch recreation drops anonymous uniques."""
	reflected = sa.Table("robot_commands", sa.MetaData(), autoload_with=bind)
	return sa.Table(
		"robot_commands",
		sa.MetaData(),
		*(
			sa.Column(
				column.name,
				column.type,
				primary_key=column.primary_key,
				nullable=column.nullable,
				server_default=column.server_default,
			)
			for column in reflected.columns
		),
	)


def _add_runtime_and_visibility(batch_op, columns: set[str]) -> None:
	if "runtime" not in columns:
		batch_op.add_column(
			sa.Column("runtime", sa.String(length=20), nullable=False, server_default="mock")
		)
	if "visibility" not in columns:
		batch_op.add_column(
			sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public")
		)


def _replace_request_id_uniqueness(bind: sa.Connection, columns: set[str]) -> None:
	"""Replace the legacy global request-id uniqueness with runtime-scoped uniqueness."""
	if bind.dialect.name == "sqlite":
		# SQLite's legacy ``unique=True`` is an anonymous autoindex. Recreate the
		# table from its columns rather than trying to drop that anonymous constraint.
		with op.batch_alter_table(
			"robot_commands",
			recreate="always",
			copy_from=_table_without_unique_constraints(bind),
		) as batch_op:
			_add_runtime_and_visibility(batch_op, columns)
			batch_op.create_unique_constraint(REQUEST_ID_UNIQUE, ["runtime", "request_id"])
		return

	_add_runtime_and_visibility(op, columns)
	inspector = sa.inspect(bind)
	for constraint in inspector.get_unique_constraints("robot_commands"):
		name = constraint.get("name")
		if constraint.get("column_names") == ["request_id"] and isinstance(name, str):
			op.drop_constraint(name, "robot_commands", type_="unique")
	for index in inspector.get_indexes("robot_commands"):
		name = index.get("name")
		if index.get("unique") and index.get("column_names") == ["request_id"] and isinstance(name, str):
			op.drop_index(name, table_name="robot_commands")
	if not _has_composite_request_id_uniqueness(sa.inspect(bind)):
		op.create_unique_constraint(REQUEST_ID_UNIQUE, "robot_commands", ["runtime", "request_id"])


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if inspector.has_table("mock_robot_commands") and not inspector.has_table("robot_commands"):
		op.rename_table("mock_robot_commands", "robot_commands")

	inspector = sa.inspect(bind)
	if not inspector.has_table("robot_commands"):
		op.create_table(
			"robot_commands",
			sa.Column("command_id", sa.String(length=36), primary_key=True),
			sa.Column("runtime", sa.String(length=20), nullable=False, server_default="mock"),
			sa.Column("request_id", sa.String(length=255), nullable=True),
			sa.Column("payload", sa.JSON(), nullable=False),
			sa.Column("status", sa.String(length=20), nullable=False),
			sa.Column("result", sa.JSON(), nullable=True),
			sa.Column("correlation_id", sa.String(length=255), nullable=True),
			sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"),
			sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
			sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
			sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
			sa.UniqueConstraint("runtime", "request_id", name=REQUEST_ID_UNIQUE),
		)
	else:
		columns = {column["name"] for column in inspector.get_columns("robot_commands")}
		_replace_request_id_uniqueness(bind, columns)

	inspector = sa.inspect(bind)
	indexes = {index["name"] for index in inspector.get_indexes("robot_commands")}
	if "ix_robot_commands_status_created" not in indexes:
		op.create_index(
			"ix_robot_commands_status_created", "robot_commands", ["status", "created_at"]
		)
	if "ix_robot_commands_completed_at" not in indexes:
		op.create_index(
			"ix_robot_commands_completed_at", "robot_commands", ["completed_at"]
		)


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if not inspector.has_table("robot_commands"):
		return
	if bind.execute(
		sa.text("SELECT 1 FROM robot_commands WHERE runtime = 'production' LIMIT 1")
	).first() is not None:
		raise RuntimeError("cannot downgrade robot_commands while production commands exist")
	indexes = {index["name"] for index in inspector.get_indexes("robot_commands")}
	for index_name in ("ix_robot_commands_status_created", "ix_robot_commands_completed_at"):
		if index_name in indexes:
			op.drop_index(index_name, table_name="robot_commands")
	columns = {column["name"] for column in inspector.get_columns("robot_commands")}
	if bind.dialect.name == "sqlite":
		with op.batch_alter_table(
			"robot_commands",
			recreate="always",
			copy_from=_table_without_unique_constraints(bind),
		) as batch_op:
			if "visibility" in columns:
				batch_op.drop_column("visibility")
			if "runtime" in columns:
				batch_op.drop_column("runtime")
			batch_op.create_unique_constraint("uq_robot_commands_request_id", ["request_id"])
	else:
		for constraint in inspector.get_unique_constraints("robot_commands"):
			name = constraint.get("name")
			if constraint.get("column_names") == ["runtime", "request_id"] and isinstance(name, str):
				op.drop_constraint(name, "robot_commands", type_="unique")
		if "visibility" in columns:
			op.drop_column("robot_commands", "visibility")
		if "runtime" in columns:
			op.drop_column("robot_commands", "runtime")
		op.create_unique_constraint("uq_robot_commands_request_id", "robot_commands", ["request_id"])
	op.rename_table("robot_commands", "mock_robot_commands")
