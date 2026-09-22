"""Add container mass and durable enrollment state."""
from alembic import op
import sqlalchemy as sa
revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None

def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "items" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("items")}
    if "tare_weight" not in existing:
        op.add_column("items", sa.Column("tare_weight", sa.Float(), nullable=True))
    if "full_net_weight" not in existing:
        op.add_column("items", sa.Column("full_net_weight", sa.Float(), nullable=True))
    if "enrollment_weight_pending" not in existing:
        op.add_column("items", sa.Column("enrollment_weight_pending", sa.Boolean(), nullable=False, server_default="0"))
    if "enrollment_storage_completed" not in existing:
        op.add_column("items", sa.Column("enrollment_storage_completed", sa.Boolean(), nullable=False, server_default="1"))
    if "processed_mass_commands" not in existing:
        op.add_column("items", sa.Column("processed_mass_commands", sa.JSON(), nullable=True))

def downgrade() -> None:
    for column in ("enrollment_storage_completed", "processed_mass_commands", "enrollment_weight_pending", "full_net_weight", "tare_weight"):
        op.drop_column("items", column)
