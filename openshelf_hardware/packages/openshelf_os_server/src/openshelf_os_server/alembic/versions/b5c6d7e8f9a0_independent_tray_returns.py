"""Separate outside trays from robot command ownership."""

from alembic import op
import sqlalchemy as sa

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    workflows = sa.table(
        "storage_bin_workflows",
        sa.column("id", sa.String),
        sa.column("bin_id", sa.Integer),
        sa.column("item_id", sa.Integer),
        sa.column("compartment", sa.Integer),
        sa.column("state", sa.String),
        sa.column("operation", sa.String),
        sa.column("history", sa.JSON),
        sa.column("resolution", sa.JSON),
    )
    # The old service committed physical success to history before updating
    # inventory. Keep those unfinished returns recoverable without replaying motion.
    physically_returned = [
        row
        for row in connection.execute(
            sa.select(workflows).where(
                workflows.c.operation.in_(["store", "retrieve", "restore"]),
                workflows.c.state.in_(
                    ["returning_bin", "recovery_required", "retry_claimed", "reconcile_claimed"]
                )
            )
        ).mappings()
        if any(entry.get("state") == "physical_return_completed" for entry in row["history"] or [])
    ]
    op.execute(
        "UPDATE storage_bins SET status = 'outside', gross_weight = NULL WHERE status != 'at_home'"
    )
    op.execute(
        "UPDATE storage_bin_workflows SET state = 'failed' WHERE state IN ('recovery_required', 'retry_claimed', 'reconcile_claimed', 'retrieving_bin', 'returning_bin')"
    )
    op.execute(
        "UPDATE storage_bin_workflows SET state = 'cancelled' WHERE operation = 'restore' "
        "AND state NOT IN ('completed', 'reconciled', 'cancelled')"
    )
    memberships = sa.table(
        "storage_bin_items",
        sa.column("bin_id", sa.Integer),
        sa.column("item_id", sa.Integer),
        sa.column("compartment", sa.Integer),
    )
    for row in physically_returned:
        resolution = dict(row["resolution"] or {})
        if row["operation"] != "restore":
            positions = {
                str(member.item_id): member.compartment
                for member in connection.execute(
                    sa.select(memberships).where(memberships.c.bin_id == row["bin_id"])
                )
            }
            confirmed_ids = {int(item_id) for item_id in positions}
            positions[str(row["item_id"])] = row["compartment"]
            target_in_bin = resolution.get("target_item_in_bin")
            if not isinstance(target_in_bin, bool):
                target_in_bin = row["operation"] == "store"
            if target_in_bin:
                confirmed_ids.add(row["item_id"])
            else:
                confirmed_ids.discard(row["item_id"])
            resolution.update(
                candidate_item_ids=[int(item_id) for item_id in positions],
                candidate_compartments=positions,
                confirmed_item_ids=sorted(confirmed_ids),
            )
        # Restore already has the complete candidate/confirmed contents snapshot.
        resolution["physical_return_completed"] = True
        connection.execute(
            workflows.update()
            .where(workflows.c.id == row["id"])
            .values(operation="return", state="returning_bin", resolution=resolution)
        )
    with op.batch_alter_table(
        "storage_bin_workflows", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
    ) as batch:
        batch.drop_constraint("uq_storage_bin_workflows_lock_key", type_="unique")
        batch.drop_column("lock_key")
        batch.drop_column("recovery_claim")
        batch.alter_column("item_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    raise RuntimeError(
        "Independent tray returns cannot be downgraded while retaining tray-only history"
    )
