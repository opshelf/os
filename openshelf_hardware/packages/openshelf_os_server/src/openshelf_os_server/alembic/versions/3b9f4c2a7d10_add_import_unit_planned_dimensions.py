"""add planned dimensions to import job units

Revision ID: 3b9f4c2a7d10
Revises: b6c7d8e9f0a1
"""

from alembic import op
import sqlalchemy as sa

revision = "3b9f4c2a7d10"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_job_units"):
        return
    if "planned_dimensions" in {column["name"] for column in inspector.get_columns("import_job_units")}:
        return
    op.add_column("import_job_units", sa.Column("planned_dimensions", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_job_units"):
        return
    if "planned_dimensions" not in {column["name"] for column in inspector.get_columns("import_job_units")}:
        return
    with op.batch_alter_table("import_job_units") as batch:
        batch.drop_column("planned_dimensions")
