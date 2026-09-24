"""Persist tray layouts and stable item/workflow compartment numbers.

Every step is applied only when it is still missing. A device whose first replay of
this revision aborted partway already holds the columns this revision adds: SQLite
commits DDL as it runs, so the batch rebuild that failed last is retried while
`storage_bin_items.compartment` and its siblings are already there. Re-adding them
raised `duplicate column name`, which pinned those devices at `f3a4b5c6d7e8` and
replayed the same abort on every start.
"""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None

COLUMNS = (
    ("storage_bins", "compartment_layout", sa.JSON()),
    ("storage_bin_items", "compartment", sa.Integer()),
    ("storage_bin_workflows", "compartment", sa.Integer()),
)
COMPARTMENT_CONSTRAINT = "uq_storage_bin_items_compartment"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    for table_name, column_name, column_type in COLUMNS:
        if not inspector.has_table(table_name):
            continue
        if column_name in {column["name"] for column in inspector.get_columns(table_name)}:
            continue
        op.add_column(table_name, sa.Column(column_name, column_type, nullable=True))

    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("storage_bin_items")
    }
    if COMPARTMENT_CONSTRAINT not in constraints:
        with op.batch_alter_table("storage_bin_items") as batch:
            batch.create_unique_constraint(COMPARTMENT_CONSTRAINT, ["bin_id", "compartment"])


def downgrade() -> None:
    with op.batch_alter_table("storage_bin_items") as batch:
        batch.drop_constraint("uq_storage_bin_items_compartment", type_="unique")
        batch.drop_column("compartment")
    op.drop_column("storage_bin_workflows", "compartment")
    op.drop_column("storage_bins", "compartment_layout")
