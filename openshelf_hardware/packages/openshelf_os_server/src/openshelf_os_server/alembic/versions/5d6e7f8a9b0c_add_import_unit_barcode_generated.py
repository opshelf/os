"""add barcode_generated to import job units

Revision ID: 5d6e7f8a9b0c
Revises: 4c1d2e3f5a6b
"""

from alembic import op
import sqlalchemy as sa

revision = "5d6e7f8a9b0c"
down_revision = "4c1d2e3f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_job_units"):
        return
    if "barcode_generated" in {column["name"] for column in inspector.get_columns("import_job_units")}:
        return
    # Units created before this revision keep their barcode: which ones were generated is unknown.
    op.add_column(
        "import_job_units",
        sa.Column("barcode_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_job_units"):
        return
    if "barcode_generated" not in {column["name"] for column in inspector.get_columns("import_job_units")}:
        return
    with op.batch_alter_table("import_job_units") as batch:
        batch.drop_column("barcode_generated")
