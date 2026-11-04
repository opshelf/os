"""add durable robot event log

Revision ID: a5b6c7d8e9f0
Revises: a4b5c6d7e8f9
"""

from alembic import op
import sqlalchemy as sa

revision = "a5b6c7d8e9f0"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("robot_events"):
        return

    op.create_table(
        "robot_events",
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_robot_events_command_id", "robot_events", ["command_id"])


def downgrade() -> None:
    op.drop_index("ix_robot_events_command_id", table_name="robot_events")
    op.drop_table("robot_events")
