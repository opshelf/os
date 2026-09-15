"""Persist tray layouts and stable item/workflow compartment numbers."""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("storage_bins", sa.Column("compartment_layout", sa.JSON(), nullable=True))
    op.add_column("storage_bin_items", sa.Column("compartment", sa.Integer(), nullable=True))
    op.add_column("storage_bin_workflows", sa.Column("compartment", sa.Integer(), nullable=True))
    with op.batch_alter_table("storage_bin_items") as batch:
        batch.create_unique_constraint("uq_storage_bin_items_compartment", ["bin_id", "compartment"])


def downgrade() -> None:
    with op.batch_alter_table("storage_bin_items") as batch:
        batch.drop_constraint("uq_storage_bin_items_compartment", type_="unique")
        batch.drop_column("compartment")
    op.drop_column("storage_bin_workflows", "compartment")
    op.drop_column("storage_bins", "compartment_layout")
