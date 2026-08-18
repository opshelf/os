"""add mock commands and audit correlation

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-08-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if inspector.has_table("audit_logs"):
		columns = {column["name"] for column in inspector.get_columns("audit_logs")}
		if "correlation_id" not in columns:
			op.add_column("audit_logs", sa.Column("correlation_id", sa.String(length=255), nullable=True))

		inspector = sa.inspect(bind)
		indexes = {index["name"] for index in inspector.get_indexes("audit_logs")}
		if "ix_audit_logs_correlation_id" not in indexes:
			op.create_index("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])

	if inspector.has_table("mock_robot_commands") is False:
		op.create_table(
			"mock_robot_commands",
			sa.Column("command_id", sa.String(length=36), primary_key=True),
			sa.Column("request_id", sa.String(length=255), nullable=True, unique=True),
			sa.Column("payload", sa.JSON(), nullable=False),
			sa.Column("status", sa.String(length=20), nullable=False),
			sa.Column("result", sa.JSON(), nullable=True),
			sa.Column("correlation_id", sa.String(length=255), nullable=True),
			sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
			sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
			sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
		)
		op.create_index(
			"ix_mock_robot_commands_status_created",
			"mock_robot_commands",
			["status", "created_at"],
		)
		op.create_index(
			"ix_mock_robot_commands_completed_at",
			"mock_robot_commands",
			["completed_at"],
		)


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if inspector.has_table("mock_robot_commands"):
		op.drop_table("mock_robot_commands")

	if inspector.has_table("audit_logs"):
		columns = {column["name"] for column in inspector.get_columns("audit_logs")}
		if "correlation_id" in columns:
			op.drop_column("audit_logs", "correlation_id")
