"""Add relay-initiated inventory import jobs and their expanded units.

Each table is created only when it is still missing: a create_all-built database
that is replayed from an older revision may already hold them.

Revision ID: a2b3c4d5e6f7
Revises: f9a0b1c2d3e4
"""

from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f7"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
	inspector = sa.inspect(op.get_bind())

	if not inspector.has_table("import_jobs"):
		op.create_table(
			"import_jobs",
			sa.Column("id", sa.String(length=36), primary_key=True),
			sa.Column("state", sa.String(length=16), nullable=False),
			sa.Column("filename", sa.String(length=255), nullable=True),
			sa.Column("cancel_source", sa.String(length=16), nullable=True),
			sa.Column("tray_workflow_id", sa.String(length=36), nullable=True),
			sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
			sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
			sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
		)
		op.create_index("ix_import_jobs_state", "import_jobs", ["state"])
		op.create_index("ix_import_jobs_created_at", "import_jobs", ["created_at"])

	if not inspector.has_table("import_job_units"):
		op.create_table(
			"import_job_units",
			sa.Column("id", sa.Integer(), primary_key=True),
			sa.Column(
				"job_id",
				sa.String(length=36),
				sa.ForeignKey("import_jobs.id", ondelete="CASCADE"),
				nullable=False,
			),
			sa.Column("position", sa.Integer(), nullable=False),
			sa.Column("row_number", sa.Integer(), nullable=True),
			sa.Column("barcode", sa.String(length=255), nullable=False),
			sa.Column("name", sa.String(length=255), nullable=False),
			sa.Column("small", sa.Boolean(), nullable=False),
			sa.Column("access_level_id", sa.Integer(), nullable=True),
			sa.Column("expires_at", sa.String(length=32), nullable=True),
			sa.Column("tare_weight", sa.Float(), nullable=True),
			sa.Column("net_weight", sa.Float(), nullable=True),
			sa.Column("full_net_weight", sa.Float(), nullable=True),
			sa.Column("unit_metadata", sa.JSON(), nullable=False),
			sa.Column("status", sa.String(length=16), nullable=False),
			sa.Column("stage", sa.String(length=16), nullable=True),
			sa.Column("skip_reason", sa.String(length=255), nullable=True),
			sa.Column("error", sa.Text(), nullable=True),
			sa.Column("request_id", sa.String(length=255), nullable=True),
			sa.Column("attempts", sa.Integer(), nullable=False),
			sa.Column("dimensions", sa.JSON(), nullable=True),
			sa.Column(
				"item_id",
				sa.Integer(),
				sa.ForeignKey("items.id", ondelete="SET NULL"),
				nullable=True,
			),
			sa.Column("bin_id", sa.Integer(), nullable=True),
			sa.Column("compartment", sa.Integer(), nullable=True),
			sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
			sa.UniqueConstraint("job_id", "position", name="uq_import_job_units_position"),
			sa.UniqueConstraint("job_id", "barcode", name="uq_import_job_units_barcode"),
		)
		op.create_index(
			"ix_import_job_units_job_status", "import_job_units", ["job_id", "status"]
		)
		op.create_index("ix_import_job_units_request_id", "import_job_units", ["request_id"])


def downgrade() -> None:
	op.drop_table("import_job_units")
	op.drop_table("import_jobs")
