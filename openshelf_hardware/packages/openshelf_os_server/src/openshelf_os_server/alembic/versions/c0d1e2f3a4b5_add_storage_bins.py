"""add multi-item storage bins

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""

from alembic import op
import sqlalchemy as sa


revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"storage_bins",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("barcode", sa.String(length=12), nullable=False, unique=True),
		sa.Column("side", sa.String(length=20), nullable=False),
		sa.Column("shelf_idx", sa.Integer(), nullable=False),
		sa.Column("cell_idx", sa.Integer(), nullable=False),
		sa.Column("inner_dimensions", sa.JSON(), nullable=False),
		sa.Column("max_items", sa.Integer(), nullable=False),
		sa.Column("status", sa.String(length=24), nullable=False),
		sa.Column("gross_weight", sa.Float(), nullable=True),
		sa.Column("active", sa.Boolean(), nullable=False),
		sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
		sa.UniqueConstraint("side", "shelf_idx", "cell_idx", name="uq_storage_bins_home_cell"),
	)
	op.create_index("ix_storage_bins_barcode", "storage_bins", ["barcode"], unique=True)
	op.create_index("ix_storage_bins_status", "storage_bins", ["status"])
	op.create_table(
		"storage_bin_items",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("bin_id", sa.Integer(), sa.ForeignKey("storage_bins.id", ondelete="CASCADE"), nullable=False),
		sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
		sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
		sa.UniqueConstraint("item_id", name="uq_storage_bin_items_item"),
	)
	op.create_index("ix_storage_bin_items_bin_id", "storage_bin_items", ["bin_id"])
	op.create_table(
		"storage_bin_workflows",
		sa.Column("id", sa.String(length=36), primary_key=True),
		sa.Column("operation", sa.String(length=12), nullable=False),
		sa.Column("state", sa.String(length=32), nullable=False),
		sa.Column("lock_key", sa.String(length=16), nullable=True, unique=True),
		sa.Column("recovery_claim", sa.String(length=36), nullable=True),
		sa.Column("bin_id", sa.Integer(), sa.ForeignKey("storage_bins.id"), nullable=False),
		sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
		sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
		sa.Column("history", sa.JSON(), nullable=False),
		sa.Column("error", sa.Text(), nullable=True),
		sa.Column("resolution", sa.JSON(), nullable=True),
		sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
	)
	op.create_index("ix_storage_bin_workflows_state", "storage_bin_workflows", ["state"])
	op.create_index("ix_storage_bin_workflows_created_at", "storage_bin_workflows", ["created_at"])


def downgrade() -> None:
	op.drop_table("storage_bin_workflows")
	op.drop_table("storage_bin_items")
	op.drop_table("storage_bins")
