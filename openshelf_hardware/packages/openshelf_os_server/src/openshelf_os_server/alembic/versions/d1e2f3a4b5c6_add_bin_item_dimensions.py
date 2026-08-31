"""add storage bin item dimensions

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""

from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column("storage_bins", sa.Column("max_item_dimensions", sa.JSON(), nullable=True))


def downgrade() -> None:
	op.drop_column("storage_bins", "max_item_dimensions")
